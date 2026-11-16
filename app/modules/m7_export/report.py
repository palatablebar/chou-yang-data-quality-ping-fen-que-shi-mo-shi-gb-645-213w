"""报告装配：把一次运行整理成报告页需要的结论、横条、柱图与对账行。

报告页只放结论与数字，规则说明在 docs/DATA-DICTIONARY.md；这里不生成
解释性长句，所有文案都由数据决定。模板字段覆盖与双口径评分（有效口径 /
核心口径）在 coverage 段集中说明，页面与导出共用同一份快照数字。
"""
from __future__ import annotations

from decimal import Decimal


def build(result: dict) -> dict:
    score = result["score"]
    dims = []
    bars = []
    for item in score.get("dims", []):
        width = (Decimal(item["ratio"]) * Decimal("100")).quantize(Decimal("0.1"))
        dims.append({**item, "width": str(min(width, Decimal("100")))})
        bars.append({
            "key": item["key"],
            "label": item["label"],
            "penalty": item["penalty"],
            "cap": item["cap"],
            "width": str(min(width, Decimal("100"))),
            "severity": item["severity"],
            "severity_label": item["severity_label"],
        })
    profile_rows = sorted(result["profile"]["profile"],
                          key=lambda item: (-Decimal(item["missing_rate"]),
                                            -Decimal(item["format_rate"]),
                                            item["name"]))
    counters = result["manifest"]["counters"]
    coverage = result.get("coverage") or _legacy_coverage(result)
    core_score = result.get("core_score")
    return {
        "run": result["run"],
        "dataset": result["dataset"],
        "score": score.get("score"),
        "grade": score.get("grade"),
        "grade_label": score.get("grade_label", ""),
        "penalty_sum": score.get("penalty_sum"),
        "closed": score.get("closed"),
        "formula": score.get("formula", ""),
        "weights": score.get("weights", []),
        "dims": dims,
        "bars": bars,
        "bands": score.get("bands", []),
        "counters": counters,
        "columns": profile_rows[:7],
        "empty_columns": result["profile"]["counts"].get("empty_columns", []),
        "accounts": result["reconcile"].get("accounts", []),
        "balanced": result["reconcile"].get("all_balanced"),
        "stages": result["manifest"].get("stages", []),
        "thresholds": result["manifest"].get("thresholds", {}),
        "manifest": result["manifest"],
        "coverage": coverage,
        "core_score": core_score,
    }


def _legacy_coverage(result: dict) -> dict | None:
    """旧快照（模板能力上线前落库的运行）没有 coverage 段时的兜底。"""
    header = (result.get("dataset") or {}).get("header") or {}
    template = header.get("template")
    if not template:
        return None
    return {
        "template_key": template.get("key", "legacy_v1"),
        "template_label": template.get("label", ""),
        "template_matched": template.get("matched", True),
        "present_columns": template.get("present", []),
        "absent_columns": template.get("absent", []),
        "columns": template.get("columns", []),
        "families": template.get("families", []),
        "policy": "旧运行快照，字段覆盖详情以重新采信结果为准。",
    }

