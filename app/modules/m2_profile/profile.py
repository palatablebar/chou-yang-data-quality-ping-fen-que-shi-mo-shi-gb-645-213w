"""字段画像：把采纳行原文逐格解析成三态单元格，再按列汇总。

输出结构是下游共享契约：
- rows[i]["cells"][column] = {state, code, raw, value, detail}
- columns 只包含本次模板实际提供的契约列（模板未提供的列不进画像、不进账），
  顺序固定为契约顺序，绝不按输入表头顺序漂移
- counts 给出 单元格 = 有值 + 格式不合格 + 缺失 的分子分母
"""
from __future__ import annotations

from ... import config
from . import column_stats, formats

# 与 m1_ingest.service.STRUCTURAL_MISSING 对应：段模板未提供该列，不进任何账
STRUCTURAL_MISSING = "\x00STRUCTURAL_MISSING"


def build(ingest: dict) -> dict:
    present = tuple(ingest["header"]["template"]["present"])
    columns = [config.COLUMN_MAP[name] for name in present]
    rows = []
    structural_cells = 0
    for row in ingest["rows"]:
        cells = {}
        for column in columns:
            raw = row["cells"].get(column.name, "")
            if raw == STRUCTURAL_MISSING:
                # 该记录所属段的模板没有这一列：结构性缺失，不占缺失/有值账
                cells[column.name] = {
                    "state": "structural_missing", "code": "K_TEMPLATE_COLUMNS_ABSENT",
                    "raw": "", "value": None,
                    "detail": "本段模板未提供该列，按缺失豁免",
                }
                structural_cells += 1
                continue
            parsed = formats.parse_cell(column, raw)
            cells[column.name] = parsed.as_dict()
        rows.append({"lineno": row["lineno"], "cells": cells,
                     "segment": row.get("segment", 1)})

    per_column = [
        column_stats.describe(column, [row["cells"][column.name] for row in rows])
        for column in columns
    ]
    missing = sum(item["missing"] for item in per_column)
    bad = sum(item["format_bad"] for item in per_column)
    # 单元格总数只含真正参与评分的格：结构性缺失格被剔除，恒等式依旧闭口
    total = len(rows) * len(columns) - structural_cells
    absent = [item["name"] for item in ingest["header"]["template"]["columns"]
              if item["status"] == "absent"]
    counts = {
        "rows": len(rows),
        "cells_total": total,
        "cells_missing": missing,
        # 有值 = 非缺失（含格式不合格）；合格 = 非缺失且格式合格
        "cells_value": total - missing,
        "cells_ok": total - missing - bad,
        "cells_format_bad": bad,
        "columns": len(columns),
        "contract_columns": len(config.COLUMNS),
        "absent_columns": absent,
        "structural_cells": structural_cells,
        "empty_columns": [item["name"] for item in per_column
                          if item["status"] == "empty_column"],
    }
    return {
        "columns": [column.as_dict() for column in columns],
        "rows": rows,
        "profile": per_column,
        "counts": counts,
    }

