"""模板识别与字段覆盖：新旧模板（受理人/区域 → 服务组）的兼容层。

设计要点（对应质量报告可比性需求）：
- 识别只按「表头映射出的契约列集合」，与物理列顺序无关：模板调整列布局、
  改别名都不会读错列（按列名取格，不按位置）。
- 模板未提供的契约列记 ``absent``：不进缺失账、不进单元格总数，所以换模板
  不会被误判成「整列缺失」的质量骤降。
- 核心列（config.CORE_COLUMNS）是所有模板的公共交集；只在核心列上重算的
  「核心口径」分用于跨模板比较，新模板里服务组等已提供列的真实缺口仍由
  「有效口径」分照扣，不会因为只比共有字段而消失。
- 校验只拦真正的结构性缺列：核心列缺失或整个字段族没有任何成员列才 422；
  同一字段族里新旧成员互不要求。
"""
from __future__ import annotations

from dataclasses import dataclass

from ... import config

# 单元格覆盖状态
PRESENT = "present"   # 模板提供该列，参与三态判定与扣分
ABSENT = "absent"     # 模板未提供该列，不进任何账、不扣分
CORE_SCOPE = "core"   # 核心口径标记（与 present 并存，不单独作为覆盖状态）


@dataclass(frozen=True)
class TemplateVerdict:
    """一次表头识别的结论。"""

    key: str
    label: str
    matched: bool
    present: tuple[str, ...]      # 模板实际提供的契约列（契约顺序）
    absent: tuple[str, ...]       # 模板未提供的契约列（契约顺序）
    core: tuple[str, ...]         # 本次实际提供的核心列
    extra_columns: tuple[str, ...]  # 映射不到契约的原始表头
    families: tuple[dict, ...]    # 字段族覆盖情况
    columns: tuple[dict, ...]     # 逐契约列覆盖行
    missing_core: tuple[str, ...]
    missing_families: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "matched": self.matched,
            "present": list(self.present),
            "absent": list(self.absent),
            "core": list(self.core),
            "extra_columns": list(self.extra_columns),
            "families": [dict(item) for item in self.families],
            "columns": [dict(item) for item in self.columns],
            "missing_core": list(self.missing_core),
            "missing_families": list(self.missing_families),
        }


def identify(indexes: dict[str, int], unknown: tuple[str, ...]) -> TemplateVerdict:
    """根据表头映射结果识别模板并产出字段覆盖账。

    ``indexes`` 是 sniff.map_header 给出的 {契约列: 物理下标}；
    ``unknown`` 是映射不到任何契约的原始表头单元格。
    """
    present_tuple = tuple(name for name in config.COLUMN_NAMES if name in indexes)
    present_set = frozenset(present_tuple)
    absent_tuple = tuple(name for name in config.COLUMN_NAMES
                         if name not in present_set)

    spec = config.TEMPLATE_BY_COLUMNS.get(present_set)
    if spec is not None:
        key, label, matched = spec.key, spec.label, True
    else:
        key, label, matched = "custom", "自定义 / 未登记模板", False

    families = _family_coverage(present_set)
    missing_families = tuple(item["family"] for item in families
                             if not item["covered"])
    missing_core = tuple(name for name in config.CORE_COLUMNS
                         if name not in present_set)

    columns = tuple(
        {
            "name": name,
            "label": config.COLUMN_MAP[name].label,
            "family": config.COLUMN_FAMILY[name],
            "status": PRESENT if name in present_set else ABSENT,
            "core": name in config.CORE_SET,
        }
        for name in config.COLUMN_NAMES
    )
    return TemplateVerdict(
        key=key, label=label, matched=matched,
        present=present_tuple, absent=absent_tuple,
        core=tuple(name for name in config.CORE_COLUMNS if name in present_set),
        extra_columns=tuple(unknown),
        families=families,
        columns=columns,
        missing_core=missing_core,
        missing_families=missing_families,
    )


def _family_coverage(present_set: frozenset[str]) -> tuple[dict, ...]:
    rows = []
    for family, members in config.FIELD_FAMILIES.items():
        present_members = tuple(name for name in members if name in present_set)
        absent_members = tuple(name for name in members if name not in present_set)
        rows.append({
            "family": family,
            "members": list(members),
            "present_members": list(present_members),
            "absent_members": list(absent_members),
            "covered": bool(present_members),
            # 字段族是跨模板等价布局：族内有任一成员即视为覆盖
            "core": all(name in config.CORE_SET for name in members),
        })
    return tuple(rows)


def validate(verdict: TemplateVerdict) -> dict | None:
    """结构性缺列校验；通过返回 None，否则返回 422 详情。"""
    if not verdict.missing_core and not verdict.missing_families:
        return None
    return {
        "missing_core": list(verdict.missing_core),
        "missing_families": list(verdict.missing_families),
        "present": list(verdict.present),
        "template": verdict.key,
    }
