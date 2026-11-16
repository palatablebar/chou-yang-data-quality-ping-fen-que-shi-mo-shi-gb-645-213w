"""运行清单导出：输入摘要、阈值、计数、对账，可离线复算。"""
from __future__ import annotations

import json

from ... import config


def build(result: dict) -> dict:
    manifest = result["manifest"]
    payload = {
        "run_id": manifest.get("run_id"),
        "version": manifest.get("version"),
        "dataset": manifest.get("dataset"),
        "rule_version": manifest.get("rule_version"),
        "rules_digest": manifest.get("rules_digest"),
        "input_digest": manifest.get("input_digest"),
        "encoding": manifest.get("encoding"),
        "declared_encoding": manifest.get("declared_encoding"),
        "params": manifest.get("params", {}),
        "thresholds": manifest.get("thresholds", {}),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "elapsed_ms": manifest.get("elapsed_ms"),
        "timezone": manifest.get("timezone"),
        "decimals": manifest.get("decimals", {}),
        "score": result["score"].get("score"),
        "grade": result["score"].get("grade"),
        "penalty_sum": result["score"].get("penalty_sum"),
        "counters": manifest.get("counters", {}),
        "accounts": manifest.get("accounts", []),
        "all_balanced": manifest.get("all_balanced"),
        "stages": manifest.get("stages", []),
        "weights": [{"key": item["key"], "cap": item["cap"],
                     "tolerance": item["tolerance"]} for item in config.WEIGHTS],
        "grades": [dict(item) for item in config.GRADES],
    }
    if result.get("core_score"):
        payload["core_score"] = {
            "score": result["core_score"]["score"],
            "grade": result["core_score"]["grade"],
            "grade_label": result["core_score"]["grade_label"],
            "scope_label": result["core_score"].get("scope_label", ""),
            "columns": result["core_score"].get("columns", list(config.CORE_COLUMNS)),
            "dims": result["core_score"]["dims"],
        }
    if result.get("coverage"):
        payload["template_coverage"] = result["coverage"]
    return payload


def to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
