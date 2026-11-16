"""重复组归并：两套账一起出，代表行与排序都与输入顺序无关。

代表行固定取最小原始行号；组排序按代表行号；同一批数据打乱行序后
分组结果、代表行、冲突列必须逐字相同。

除模板有效口径的全行哈希账外，另出一份只含核心列的哈希账（core_hash）：
核心列是所有模板的公共交集，核心口径的重复率用于跨模板比较。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

from ... import config
from . import hashdup, keydup


def _core_row_hash(profile: dict, row: dict) -> str:
    payload = {
        name: hashdup.canonical_cell(row["cells"][name])
        for name in config.CORE_COLUMNS
        if name in row["cells"]
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def core_group(profile: dict) -> dict:
    """只按核心列算全行重复：跨模板可比口径的重复分子分母。"""
    buckets: dict[str, list[int]] = {}
    for row in profile["rows"]:
        buckets.setdefault(_core_row_hash(profile, row), []).append(row["lineno"])
    duplicate_rows = sum(len(linenos) - 1 for linenos in buckets.values()
                         if len(linenos) >= 2)
    total = len(profile["rows"])
    return {
        "rows": total,
        "unique_rows": total - duplicate_rows,
        "duplicate_rows": duplicate_rows,
        "columns": list(config.CORE_COLUMNS),
    }


def build(profile: dict) -> dict:
    by_hash = hashdup.group(profile)
    by_key = keydup.group(profile)
    by_core = core_group(profile)
    total = by_hash["rows"]
    return {
        "hash": by_hash,
        "key": by_key,
        "core_hash": by_core,
        "rows": total,
        "unique_rows": by_hash["unique_rows"],
        "duplicate_rows": by_hash["duplicate_rows"],
        "hash_groups": by_hash["groups"],
        "key_repeats": by_key["repeats"],
        "conflicts": by_key["conflicts"],
        "duplicate_rate": _rate(by_hash["duplicate_rows"], total),
        "conflict_rate": _rate(len(by_key["conflicts"]), total),
        "identity_columns": by_key["identity_columns"],
    }


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


def representative(rows: list[dict]) -> int | None:
    if not rows:
        return None
    return min(row["lineno"] for row in rows)
