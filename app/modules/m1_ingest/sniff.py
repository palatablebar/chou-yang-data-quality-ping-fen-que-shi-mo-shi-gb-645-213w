"""表头识别、重复表头分段、列数校验、截断判定与空态三分。

约定：
- 表头按别名映射到契约列名，缺必填列直接 422；
- 数据区里再出现一次表头签名 → 记一段，不是一条数据；
- 全空单元格的行记空行，不进隔离也不进采纳；
- 列数与表头不一致 → 隔离；若同时是「文件末行且无换行」→ 判为截断。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ... import config, reasons
from ...errors import UnprocessableInput
from . import templates as template_mod
from .reader import Record, ends_with_newline

HEADER_MARKS = ("\ufeff", "\u200b")


def clean_header_cell(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    for mark in HEADER_MARKS:
        text = text.replace(mark, "")
    return text.strip()


def header_signature(cells: list[str]) -> tuple[str, ...]:
    return tuple(clean_header_cell(cell).lower() for cell in cells)


@dataclass(frozen=True)
class HeaderInfo:
    cells: list[str]
    signature: tuple[str, ...]
    indexes: dict[str, int]
    missing: tuple[str, ...]
    unknown: tuple[str, ...]
    aliased: tuple[str, ...]
    template: template_mod.TemplateVerdict

    @property
    def width(self) -> int:
        return len(self.cells)

    @property
    def present_columns(self) -> tuple[str, ...]:
        """模板实际提供的契约列；下游所有账只在这些列上计算。"""
        return self.template.present

    def value(self, record: Record, column: str) -> str:
        index = self.indexes.get(column)
        if index is None or index >= len(record.cells):
            return ""
        return record.cells[index]

    def as_dict(self) -> dict:
        return {"cells": self.cells, "missing": list(self.missing),
                "unknown": list(self.unknown), "aliased": list(self.aliased),
                "width": self.width, "template": self.template.as_dict()}


def map_header(cells: list[str]) -> HeaderInfo:
    normalized = {name: name for name in config.COLUMN_NAMES}
    lookup: dict[str, str] = {}
    for name, aliases in config.ALIASES.items():
        for alias in aliases:
            lookup[clean_header_cell(alias).lower()] = name
    indexes: dict[str, int] = {}
    aliased: list[str] = []
    unknown: list[str] = []
    for index, cell in enumerate(cells):
        key = clean_header_cell(cell).lower()
        column = lookup.get(key)
        if column is None:
            unknown.append(cell)
            continue
        if column not in indexes:
            indexes[column] = index
        if clean_header_cell(cell) != normalized.get(column, column):
            aliased.append(cell)
    missing = tuple(name for name in config.REQUIRED_COLUMNS if name not in indexes)
    verdict = template_mod.identify(indexes, tuple(unknown))
    return HeaderInfo(cells=[clean_header_cell(cell) for cell in cells],
                      signature=header_signature(cells), indexes=indexes,
                      missing=missing, unknown=tuple(unknown), aliased=tuple(aliased),
                      template=verdict)


def triage(text: str, records: list[Record]) -> None:
    """空态三分 + 表头缺失，各给独立原因码，不留半批数据。"""
    if not text.strip():
        raise UnprocessableInput("文件没有内容", code="E_EMPTY_FILE",
                                  detail={"reason": reasons.label("E_EMPTY_FILE")})
    if not records:
        raise UnprocessableInput("只有换行，没有表头", code="E_HEADER_ONLY",
                                  detail={"reason": reasons.label("E_HEADER_ONLY")})


@dataclass(frozen=True)
class Parsed:
    header: HeaderInfo
    segments: int
    rows: list[Record]
    quarantine: list[dict]
    blanks: list[int]
    flags: list[dict]
    tail_no_newline: bool
    segment_headers: dict[int, HeaderInfo]
    row_segments: dict[int, int]


def quarantine_entry(record: Record, code: str, segment: int, detail: str) -> dict:
    return {
        "lineno": record.lineno,
        "segment": segment,
        "code": code,
        "stage": "ingest",
        "level": reasons.level(code),
        "field": "row",
        "raw_value": ",".join(record.cells)[:300],
        "detail": detail,
    }


def parse(text: str, records: list[Record]) -> Parsed:
    triage(text, records)
    header = map_header(records[0].cells)
    structural = template_mod.validate(header.template)
    if structural is not None:
        raise UnprocessableInput(
            "表头缺少必填列：" + "、".join(
                structural["missing_core"] + structural["missing_families"]),
            code="E_HEADER_INVALID",
            detail={"missing": structural["missing_core"] + structural["missing_families"],
                    "missing_core": structural["missing_core"],
                    "missing_families": structural["missing_families"],
                    "template": structural["template"],
                    "header": header.cells})
    tail_no_newline = not ends_with_newline(text)
    flags: list[dict] = []
    if header.aliased:
        flags.append({"code": "K_ALIAS_MAPPED", "lineno": 1,
                      "detail": "、".join(header.aliased)})
    if header.template.absent:
        flags.append({
            "code": "K_TEMPLATE_COLUMNS_ABSENT", "lineno": 1,
            "detail": "模板未提供列（按缺失豁免，不计扣分）："
                      + "、".join(header.template.absent),
        })
    if not header.template.matched:
        flags.append({
            "code": "K_TEMPLATE_UNREGISTERED", "lineno": 1,
            "detail": f"未登记模板 {header.template.key}，按实际提供列评分",
        })
    if tail_no_newline:
        flags.append({"code": "K_TRUNCATED_TAIL", "lineno": records[-1].lineno,
                      "detail": "文件末行没有换行符"})

    segments = 1
    # 每一段各自的表头：新旧模板列布局不同，取格必须按该记录所属段的表头映射，
    # 不能统一按首段表头（否则服务组段会读错列）。
    segment_headers: dict[int, HeaderInfo] = {1: header}
    segment_widths = {1: header.width}
    rows: list[Record] = []
    quarantine: list[dict] = []
    blanks: list[int] = []
    data_records: list[tuple[Record, int]] = []
    current_segment = 1
    for record in records[1:]:
        signature = header_signature(record.cells)
        embedded = _embedded_header(record)
        if len(record.cells) > 1 and (signature == header.signature or embedded):
            # 同签名重复表头，或数据区里出现另一种登记模板的表头（新旧模板混排）
            current_segment = segments + 1
            segment_header = map_header(record.cells)
            segment_headers[current_segment] = segment_header
            segment_widths[current_segment] = len(record.cells)
            segments += 1
            if embedded and embedded["key"] != header.template.key:
                flags.append({
                    "code": "K_TEMPLATE_SWITCH", "lineno": record.lineno,
                    "detail": f"段内切换模板：{header.template.key} → {embedded['key']}",
                })
            flags.append({"code": "K_SEGMENT_SPLIT", "lineno": record.lineno,
                          "detail": f"第 {segments} 段表头"})
            continue
        if record.blank:
            blanks.append(record.lineno)
            flags.append({"code": "K_BLANK_ROW", "lineno": record.lineno,
                          "detail": "整行为空"})
            continue
        data_records.append((record, current_segment))

    if not data_records and not blanks:
        raise UnprocessableInput("只有表头，没有数据行", code="E_HEADER_ONLY",
                                  detail={"reason": reasons.label("E_HEADER_ONLY")})
    if not data_records:
        raise UnprocessableInput("数据行全部为空", code="E_ALL_BLANK",
                                  detail={"reason": reasons.label("E_ALL_BLANK")})

    last_lineno = data_records[-1][0].lineno
    row_segments: dict[int, int] = {}
    for record, segment in data_records:
        expected_width = segment_widths.get(segment, header.width)
        if len(record.cells) == expected_width:
            rows.append(record)
            row_segments[record.lineno] = segment
            continue
        if record.lineno == last_lineno and tail_no_newline:
            quarantine.append(quarantine_entry(
                record, "Q_TRUNCATED_ROW", segment,
                f"末行无换行且列数 {len(record.cells)}≠{expected_width}"))
        else:
            quarantine.append(quarantine_entry(
                record, "Q_COLUMN_COUNT", segment,
                f"列数 {len(record.cells)}≠段表头 {expected_width}"))
    return Parsed(header=header, segments=segments, rows=rows,
                  quarantine=quarantine, blanks=blanks, flags=flags,
                  tail_no_newline=tail_no_newline,
                  segment_headers=segment_headers, row_segments=row_segments)


def _embedded_header(record: Record) -> dict | None:
    """数据区里的一行是否是另一种登记模板的表头。

    按别名重新映射该行；命中任一已登记模板（且没有结构缺列）即认定为分段表头，
    这样新旧模板混排时第二段不会被当成坏数据隔离，也不会读错列。
    """
    candidate = map_header(record.cells)
    if candidate.template.matched and not candidate.template.missing_core \
            and not candidate.template.missing_families:
        return {"key": candidate.template.key,
                "present": list(candidate.template.present)}
    return None
