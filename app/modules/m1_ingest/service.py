"""M1 采信编排：字节 → 解码 → 记录 → 表头/分段/列数 → 逐行诊断。

输出是下游唯一入口：header 契约索引 + 采纳行原文 + 隔离行 + 标记 + 计数。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ... import config, manifest, reasons
from ...errors import NotFound, UnprocessableInput
from . import decode, reader, sniff, templates

# 结构性缺失哨兵：某段模板未提供该列（区别于真实空值），M2 见此不计入缺失账
STRUCTURAL_MISSING = "\x00STRUCTURAL_MISSING"


def available_samples(settings: config.Settings) -> list[dict]:
    sample_dir = Path(settings.sample_dir)
    items = []
    for name, label in config.SAMPLES.items():
        path = sample_dir / name
        items.append({"name": name, "label": label, "exists": path.exists(),
                      "bytes": path.stat().st_size if path.exists() else 0})
    return items


def sample_bytes(settings: config.Settings, name: str) -> bytes:
    safe = Path(name).name
    if safe not in config.SAMPLES:
        raise NotFound(f"样例不存在：{name}", code="E_SAMPLE_UNKNOWN")
    path = Path(settings.sample_dir) / safe
    if not path.exists():
        raise NotFound(f"样例文件缺失：{safe}", code="E_SAMPLE_UNKNOWN")
    return path.read_bytes()


def ingest(settings: config.Settings, *, name: str | None = None,
           data: bytes | None = None, declared: str | None = None,
           store=None) -> dict:
    """采信一次；返回可 JSON 化的数据结构，调用方负责落库。

    名字优先按样例文件解析；样例里没有就回查已存数据集（上传过的文件
    可以直接按名字重跑），两处都没有才是 404。
    """
    if data is None:
        if not name:
            raise UnprocessableInput("需要样例名或上传内容", code="E_EMPTY_UPLOAD")
        dataset_name = Path(name).name
        if dataset_name in config.SAMPLES:
            data = sample_bytes(settings, dataset_name)
        else:
            stored = store.get_dataset(dataset_name) if store is not None else None
            if stored is None:
                raise NotFound(f"样例不存在：{name}", code="E_SAMPLE_UNKNOWN")
            data = stored["content"]
            declared = declared or stored["declared_encoding"]
    else:
        if not data:
            raise UnprocessableInput("上传内容为空", code="E_EMPTY_UPLOAD")
        dataset_name = Path(name or "upload.csv").name
    decoded = decode.decode_bytes(data, declared)
    delimiter = reader.pick_delimiter(decoded.text)
    records = reader.iter_records(decoded.text, delimiter)
    parsed = sniff.parse(decoded.text, records)
    flags = [{"code": code, "lineno": 0, "detail": ""} for code in decoded.flags]
    flags.extend(parsed.flags)

    rows = []
    # 混排文件按每段自己的表头取格（新旧模板列布局不同），再把各段提供列取并集，
    # 得到整批的字段覆盖；单模板文件并集就是该模板的列。
    present_set: set[str] = set()
    for segment_header in parsed.segment_headers.values():
        present_set.update(segment_header.present_columns)
    present_columns = tuple(name for name in config.COLUMN_NAMES
                            if name in present_set)
    for record in parsed.rows:
        segment = parsed.row_segments.get(record.lineno, 1)
        segment_header = parsed.segment_headers.get(segment, parsed.header)
        segment_present = segment_header.present_columns
        cells = {}
        for column in present_columns:
            if column in segment_present:
                # 该段提供此列：保留原值（含真实空串），交给 M2 做三态判定
                cells[column] = segment_header.value(record, column)
            else:
                # 该段模板未提供此列：结构性缺失，统一哨兵，M2 识别后不计入缺失账
                cells[column] = STRUCTURAL_MISSING
        rows.append({"lineno": record.lineno, "cells": cells, "segment": segment})

    template = _union_template(parsed, present_columns)

    rename = bool(dataset_name != (name or dataset_name))
    counts = {
        "raw_records": len(parsed.rows) + len(parsed.quarantine) + len(parsed.blanks),
        "accepted": len(parsed.rows),
        "quarantine": len(parsed.quarantine),
        "blank": len(parsed.blanks),
        "segments": parsed.segments,
        "columns": parsed.header.width,
        "present_columns": len(present_columns),
        "absent_columns": len(template["absent"]),
        "bytes": len(data),
    }
    header_payload = parsed.header.as_dict()
    # 整批口径的模板识别（各段并集）覆盖掉首段单模板结论，供下游统一使用
    header_payload["template"] = template
    return {
        "name": dataset_name,
        "digest": manifest.digest_bytes(data),
        "encoding": decoded.encoding,
        "declared_encoding": declared,
        "delimiter": delimiter,
        "segments": parsed.segments,
        "header": header_payload,
        "segment_templates": [
            {"segment": segment, "key": info.template.key,
             "label": info.template.label,
             "present": list(info.template.present)}
            for segment, info in sorted(parsed.segment_headers.items())
        ],
        "rows": rows,
        "quarantine": parsed.quarantine,
        "flags": flags,
        "counts": counts,
        "text": decoded.text,
        "raw": data,
        "renamed": rename,
    }


def _union_template(parsed, present_columns: tuple[str, ...]) -> dict:
    """整批模板结论：各段表头提供列取并集后重新识别。

    单模板文件与首段结论一致；新旧模板混排时并集会同时包含 agent/region 与
    service_group，识别为未登记的混合布局（custom），但字段族仍判为已覆盖。
    """
    indexes = {name: index for index, name in enumerate(present_columns)}
    unknown: list[str] = []
    for info in parsed.segment_headers.values():
        unknown.extend(info.unknown)
    verdict = templates.identify(indexes, tuple(dict.fromkeys(unknown)))
    return verdict.as_dict()


def quarantine_summary(quarantine: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for item in quarantine:
        bucket = buckets.setdefault(item["code"], {
            "code": item["code"], "label": reasons.label(item["code"]),
            "count": 0, "linenos": []})
        bucket["count"] += 1
        if len(bucket["linenos"]) < 12:
            bucket["linenos"].append(item["lineno"])
    return sorted(buckets.values(), key=lambda item: (-item["count"], item["code"]))


def flag_summary(flags: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for item in flags:
        bucket = buckets.setdefault(item["code"], {
            "code": item["code"], "label": reasons.label(item["code"]),
            "level": reasons.level(item["code"]), "count": 0})
        bucket["count"] += 1
    return sorted(buckets.values(), key=lambda item: (-item["count"], item["code"]))


def preview(settings: config.Settings, name: str, limit: int = 40) -> dict:
    payload = ingest(settings, name=name)
    return {
        "name": payload["name"],
        "digest": payload["digest"],
        "counts": payload["counts"],
        "header": payload["header"],
        "rows": payload["rows"][:limit],
        "quarantine": payload["quarantine"][:limit],
        "flags": flag_summary(payload["flags"]),
        "timestamp": dt.datetime.now(config.UTC).isoformat(timespec="seconds"),
    }
