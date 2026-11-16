"""模板字段覆盖账：逐列说明覆盖状态及其对扣分的影响。

回答两个对称的问题：
1. 模板未提供的列若被错误当成缺失，会虚增多少缺失格、虚扣多少分（豁免影响）；
2. 模板已提供列里的真实缺口实际被扣了多少分（真实影响）——新模板的真实
   缺口因此不会因为「只比共有字段」而消失。

所有数字都能追回 profile / score 的分子分母，只做汇总不另立规则。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config


def _column_stats(profile: dict) -> dict[str, dict]:
    return {item["name"]: item for item in profile["profile"]}


def _dim_map(scored: dict) -> dict[str, dict]:
    return {item["key"]: item for item in scored["dims"]}


def build_coverage(ingest: dict, profile: dict, scored: dict,
                   core_scored: dict, rows_accepted: int) -> dict:
    verdict = ingest["header"]["template"]
    stats = _column_stats(profile)
    missing_cap = Decimal(config.WEIGHT_MAP["missing"]["cap"])
    missing_tol = Decimal(config.WEIGHT_MAP["missing"]["tolerance"])
    effective_cells = profile["counts"]["cells_total"]
    # 混排批次里部分段不提供某列，会产生逐格结构性缺失
    structural_cells = profile["counts"].get("structural_cells", 0)

    columns = []
    exempted_cells = structural_cells
    for item in verdict["columns"]:
        name = item["name"]
        if item["status"] == "absent":
            # 模板未提供：若按旧逻辑当整列缺失，会虚增 rows_accepted 个缺失格
            virtual_missing = rows_accepted
            exempted_cells += virtual_missing
            columns.append({
                "name": name, "label": item["label"], "family": item["family"],
                "status": "absent", "core": item["core"],
                "missing": 0, "missing_rate": "0.0000",
                "format_bad": 0, "structural": 0,
                "detail": "模板未提供，按缺失豁免，不进单元格账、不扣分",
                "virtual_missing_if_naive": virtual_missing,
            })
            continue
        stat = stats[name]
        column_structural = stat.get("structural", 0)
        columns.append({
            "name": name, "label": item["label"], "family": item["family"],
            "status": "present", "core": item["core"],
            "missing": stat["missing"],
            "missing_rate": str(stat["missing_rate"]),
            "format_bad": stat["format_bad"],
            "structural": column_structural,
            "detail": ("模板已提供，空值与格式问题按规则照扣"
                       + (f"；另有 {column_structural} 格属他段模板未提供，已豁免"
                          if column_structural else "")),
            "virtual_missing_if_naive": column_structural,
        })

    # 朴素口径（错误做法）：把未提供列当整列缺失时的缺失率与虚扣分
    naive_cells = effective_cells + exempted_cells
    naive_missing = profile["counts"]["cells_missing"] + exempted_cells
    naive_rate = _rate(naive_missing, naive_cells)
    naive_penalty = _missing_penalty(naive_rate, missing_cap, missing_tol)
    effective_missing_penalty = Decimal(_dim_map(scored)["missing"]["penalty"])
    inflated_penalty = naive_penalty - effective_missing_penalty

    # 核心口径与有效口径的总分差：完全由模板专属列（非核心的已提供列）贡献
    score_gap = Decimal(scored["score"]) - Decimal(core_scored["score"])

    families = []
    for family in verdict["families"]:
        families.append({
            "family": family["family"],
            "covered": family["covered"],
            "core": family["core"],
            "present_members": family["present_members"],
            "absent_members": family["absent_members"],
            "detail": _family_detail(family),
        })

    return {
        "template_key": verdict["key"],
        "template_label": verdict["label"],
        "template_matched": verdict["matched"],
        "present_columns": list(verdict["present"]),
        "absent_columns": list(verdict["absent"]),
        "core_columns": list(config.CORE_COLUMNS),
        "extra_columns": list(verdict["extra_columns"]),
        "columns": columns,
        "families": families,
        "effective": {
            "scope": "effective",
            "scope_label": "有效口径（模板实际提供的全部列）",
            "cells_total": effective_cells,
            "cells_missing": profile["counts"]["cells_missing"],
            "score": scored["score"],
            "grade": scored["grade"],
            "missing_penalty": str(effective_missing_penalty),
        },
        "core": {
            "scope": "core",
            "scope_label": "核心口径（所有模板共有列，用于跨模板比较）",
            "score": core_scored["score"],
            "grade": core_scored["grade"],
            "missing_penalty": _dim_map(core_scored)["missing"]["penalty"],
        },
        "naive_comparison": {
            "cells_total_if_absent_counted_as_missing": naive_cells,
            "missing_if_absent_counted_as_missing": naive_missing,
            "missing_rate_if_naive": str(naive_rate),
            "missing_penalty_if_naive": str(naive_penalty),
            "inflated_missing_penalty": str(inflated_penalty.quantize(
                config.SCORE_QUANT, rounding=ROUND_HALF_UP)),
            "detail": "把模板未提供列误当整列缺失会产生的虚高缺失扣分",
        },
        "score_gap_effective_minus_core": str(score_gap.quantize(
            config.SCORE_QUANT, rounding=ROUND_HALF_UP)),
        "exempted_cells": exempted_cells,
        "policy": (
            "模板未提供列记 absent：不进缺失账、不进单元格总数、不扣分；"
            "已提供列的真实空值与格式问题照常扣分。有效口径分对本模板负责，"
            "核心口径分用于新旧模板之间的横向与纵向比较。"
        ),
    }


def _family_detail(family: dict) -> str:
    if not family["covered"]:
        return f"字段族 {family['family']} 没有任何成员列，整批拒绝采信"
    if not family["absent_members"]:
        return "字段族全部成员列均已提供"
    return ("已提供 " + "、".join(family["present_members"])
            + "；未提供 " + "、".join(family["absent_members"])
            + "（同族等价布局，互不视为缺失）")


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


def _missing_penalty(rate_value: Decimal, cap: Decimal,
                     tolerance: Decimal) -> Decimal:
    ratio = (rate_value / tolerance) if tolerance > 0 else Decimal("0")
    ratio = min(ratio, Decimal("1"))
    return (cap * ratio).quantize(config.SCORE_QUANT, rounding=ROUND_HALF_UP)
