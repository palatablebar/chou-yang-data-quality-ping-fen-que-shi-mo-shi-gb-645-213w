"""新旧模板兼容：模板识别、字段覆盖、双口径评分、混排、兼容性与幂等。

覆盖需求：
- 模板未提供列不得当缺失（否则模板切换像质量骤降）；
- 只比共有字段会让新模板真实缺口消失，故有效口径照扣、核心口径另算可比口径；
- 旧字段改名/列布局调整保持兼容，按列名映射不读错列；
- 整批重跑不新增版本、不丢记录；核心列缺失仍 422。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import config
from app.errors import UnprocessableInput
from app.modules.m1_ingest import service as ingest_service
from app.modules.m6_score import service as score_service
from fixtures import dirty_cases

V2_HEADER = ("ticket_id,customer_id,channel,created_at,first_response_min,"
             "handle_hours,satisfaction,service_group,priority")


def v2_row(ticket="T-2026-300001", customer="00501", channel="电话",
           created="2026-08-01T09:00:00+08:00", response="20.0",
           handle="2.50", satisfaction="4", service_group="华东一组",
           priority="P2") -> str:
    return dirty_cases.quote(ticket, customer, channel, created, response,
                             handle, satisfaction, service_group, priority)


def v2_frame(rows: list[str]) -> str:
    return V2_HEADER + "\n" + "\n".join(rows) + "\n"


# ---------- M1：模板识别与豁免 ----------

def test_v2_template_detected_and_absent_columns_exempted(settings):
    text = v2_frame([v2_row(), v2_row(ticket="T-2026-300002", customer="00502")])
    payload = ingest_service.ingest(settings, name="v2.csv", data=text.encode())
    verdict = payload["header"]["template"]
    assert verdict["key"] == "service_v2"
    assert verdict["matched"] is True
    assert verdict["present"] == list(config.TEMPLATE_MAP["service_v2"].columns)
    assert verdict["absent"] == ["agent", "region"]
    # 未提供列根本不在行单元格里，不会被当空值
    assert "agent" not in payload["rows"][0]["cells"]
    assert payload["counts"]["present_columns"] == 9
    assert payload["counts"]["absent_columns"] == 2
    codes = [flag["code"] for flag in payload["flags"]]
    assert "K_TEMPLATE_COLUMNS_ABSENT" in codes


def test_legacy_template_still_recognized(settings):
    payload = ingest_service.ingest(settings, name="tickets_dirty.csv")
    verdict = payload["header"]["template"]
    assert verdict["key"] == "legacy_v1"
    assert verdict["absent"] == ["service_group"]


def test_missing_core_column_still_rejected(settings):
    # 去掉核心列 priority：新旧模板都必须提供核心列
    header = V2_HEADER.rsplit(",", 1)[0]
    text = header + "\n" + v2_row().rsplit(",", 1)[0] + "\n"
    with pytest.raises(UnprocessableInput) as info:
        ingest_service.ingest(settings, name="bad.csv", data=text.encode())
    assert info.value.code == "E_HEADER_INVALID"
    assert "priority" in info.value.detail["missing_core"]


def test_service_owner_family_any_member_accepted(settings):
    # 既没有 agent/region 也没有 service_group → 整字段族缺失，422
    header = ("ticket_id,customer_id,channel,created_at,first_response_min,"
              "handle_hours,satisfaction,priority")
    text = header + "\n" + ",".join(["T-2026-300001", "00501", "电话",
        "2026-08-01T09:00:00+08:00", "20.0", "2.50", "4", "P2"]) + "\n"
    with pytest.raises(UnprocessableInput) as info:
        ingest_service.ingest(settings, name="nofamily.csv", data=text.encode())
    assert info.value.code == "E_HEADER_INVALID"
    assert "service_owner" in info.value.detail["missing_families"]


def test_column_reorder_does_not_misread_columns(settings):
    # 布局调整：把 service_group 挪到首列，按列名映射，值不能错位
    rows = [v2_row(ticket="T-2026-300001", service_group="华南九组")]
    header = ("service_group,ticket_id,customer_id,channel,created_at,"
              "first_response_min,handle_hours,satisfaction,priority")
    # 手工按新顺序拼一行
    cells = rows[0].split(",")
    reordered = ",".join([cells[7], cells[0], cells[1], cells[2], cells[3],
                          cells[4], cells[5], cells[6], cells[8]])
    text = header + "\n" + reordered + "\n"
    payload = ingest_service.ingest(settings, name="reorder.csv",
                                    data=text.encode())
    verdict = payload["header"]["template"]
    assert verdict["key"] == "service_v2"
    row = payload["rows"][0]["cells"]
    assert row["service_group"] == "华南九组"
    assert row["ticket_id"] == "T-2026-300001"
    assert row["priority"] == "P2"


def test_alias_headers_map_to_v2_template(settings):
    # 中文别名表头也要识别成新模板
    header = ("工单号,客户号,渠道,受理时间,首响(分钟),处理时长(小时),满意度,"
              "服务组,优先级")
    text = header + "\n" + v2_row() + "\n"
    payload = ingest_service.ingest(settings, name="alias.csv",
                                    data=text.encode())
    assert payload["header"]["template"]["key"] == "service_v2"
    assert payload["rows"][0]["cells"]["ticket_id"] == "T-2026-300001"


def test_extra_unknown_columns_keep_template_match(settings):
    # 登记模板的列齐全，再多出未知列：模板照常命中，未知列登记不报错（前向兼容）
    header = V2_HEADER + ",extra_note"
    text = header + "\n" + v2_row() + ",备注\n"
    payload = ingest_service.ingest(settings, name="extra.csv",
                                    data=text.encode())
    verdict = payload["header"]["template"]
    assert verdict["key"] == "service_v2" and verdict["matched"] is True
    assert verdict["extra_columns"] == ["extra_note"]


def test_unregistered_layout_is_flagged_but_scored(settings, store):
    # 只有 agent 没有 region/service_group 的部分布局：核心齐全、字段族覆盖，
    # 识别为未登记模板并标 K_TEMPLATE_UNREGISTERED，仍按实际列完成评分
    header = ("ticket_id,customer_id,channel,created_at,first_response_min,"
              "handle_hours,satisfaction,agent,priority")
    text = header + "\n" + dirty_cases.quote(
        "T-2026-400002", "00802", "电话", "2026-10-01T09:00:00+08:00",
        "20.0", "2.50", "4", "陈允", "P2") + "\n"
    result = score_service.run(settings, store, name="partial.csv",
                               data=text.encode())
    assert result["coverage"]["template_matched"] is False
    codes = [flag["code"] for flag in result["dataset"]["flags"]]
    assert "K_TEMPLATE_UNREGISTERED" in codes
    assert result["reconcile"]["all_balanced"] is True


# ---------- 双口径评分 ----------

def test_effective_score_counts_real_gaps_but_not_absent_columns(settings, store):
    # 12 行 v2，其中 2 行 service_group 真空；agent/region 整列未提供
    rows = [v2_row(ticket=f"T-2026-31{index:04d}", customer=f"006{index:02d}",
                   service_group="" if index in (3, 7) else "华东一组")
            for index in range(12)]
    result = score_service.run(settings, store, name="v2gaps.csv",
                               data=v2_frame(rows).encode())
    cov = result["coverage"]
    # 有效口径：9 列 × 12 行 = 108 格，未提供的 agent/region 不进总数
    assert result["profile"]["counts"]["cells_total"] == 108
    # service_group 的 2 个真实空值被计为缺失，没有被豁免吃掉
    sg = next(item for item in result["profile"]["profile"]
              if item["name"] == "service_group")
    assert sg["missing"] == 2
    assert result["profile"]["counts"]["cells_missing"] == 2
    # 朴素口径会把 24 个 agent/region 格误当缺失，虚扣必须为正且被本系统避免
    assert cov["naive_comparison"]["missing_if_absent_counted_as_missing"] == 26
    assert Decimal(cov["naive_comparison"]["inflated_missing_penalty"]) > 0
    assert cov["exempted_cells"] == 24
    # 核心口径只算 8 个核心列，不含服务归属字段族
    assert result["core_score"]["columns"] == list(config.CORE_COLUMNS)
    assert Decimal(result["core_score"]["score"]) >= Decimal(result["score"]["score"])


def test_core_score_is_comparable_across_templates(settings, store):
    # 同样的核心数据，分别用旧模板和新模板喂入，核心口径分必须一致
    core_kwargs = dict(channel="在线", response="22.0", handle="3.00",
                       satisfaction="4", priority="P2")
    legacy_rows = [dirty_cases.make_row(ticket=f"T-2026-32{index:04d}",
                                        customer=f"007{index:02d}",
                                        agent="陈允", region="华东", **core_kwargs)
                   for index in range(8)]
    v2_rows = [v2_row(ticket=f"T-2026-32{index:04d}", customer=f"007{index:02d}",
                      service_group="华东一组",
                      **{k: v for k, v in core_kwargs.items()
                         if k not in ("agent", "region")})
               for index in range(8)]
    legacy = score_service.run(settings, store, name="cmp_legacy.csv",
                               data=dirty_cases.frame(legacy_rows).encode())
    v2 = score_service.run(settings, store, name="cmp_v2.csv",
                           data=v2_frame(v2_rows).encode())
    assert legacy["core_score"]["score"] == v2["core_score"]["score"]
    assert legacy["core_score"]["grade"] == v2["core_score"]["grade"]


def test_score_closure_holds_for_v2(settings, store):
    result = score_service.run(settings, store, name="tickets_service_v2.csv")
    total = Decimal(result["score"]["score"])
    penalty = Decimal(result["score"]["penalty_sum"])
    assert total + penalty == Decimal("100")
    core_total = Decimal(result["core_score"]["score"])
    core_penalty = Decimal(result["core_score"]["penalty_sum"])
    assert core_total + core_penalty == Decimal("100")
    assert result["reconcile"]["all_balanced"] is True


# ---------- 混排批次 ----------

def test_mixed_template_batch_segments_and_structural_cells(settings, store):
    result = score_service.run(settings, store,
                               name="tickets_mixed_templates.csv")
    # 两段、两种模板，不隔离任何数据行
    assert result["dataset"]["segments"] == 2
    codes = [flag["code"] for flag in result["dataset"]["flags"]]
    assert "K_TEMPLATE_SWITCH" in codes
    assert "K_SEGMENT_SPLIT" in codes
    assert result["run"]["rows_quarantine"] == 0
    cov = result["coverage"]
    assert cov["template_matched"] is False  # 并集布局未登记
    # 三个服务归属列都在并集里，字段族仍判覆盖
    families = {item["family"]: item for item in cov["families"]}
    assert families["service_owner"]["covered"] is True
    # 4 行旧段没有 service_group、5 行新段没有 agent/region → 14 个结构格
    assert result["profile"]["counts"]["structural_cells"] == 14
    # 结构格不进单元格账：9 行 × 11 列 − 14 = 85
    assert result["profile"]["counts"]["cells_total"] == 85
    # 真实空值（旧段 1 个 region、新段 1 个 service_group）仍被计缺失
    assert result["profile"]["counts"]["cells_missing"] == 2


def test_mixed_batch_does_not_misread_columns(settings, store):
    result = score_service.run(settings, store,
                               name="tickets_mixed_templates.csv")
    by_lineno = {row["lineno"]: row["cells"]
                 for row in result["profile"]["rows"]}
    # 新段首行（源文件第 7 行）service_group 不能读到旧段的 region/agent 列位
    new_row = by_lineno[7]
    assert new_row["service_group"]["value"] == "华东一组"
    assert new_row["ticket_id"]["value"] == "T-2026-200101"
    old_row = by_lineno[2]
    assert old_row["agent"]["value"] == "陈允"
    assert old_row["region"]["value"] == "华东"
    # 跨段未提供列标为结构性缺失，而不是数据缺失
    assert new_row["agent"]["state"] == "structural_missing"
    assert old_row["service_group"]["state"] == "structural_missing"


# ---------- 幂等与版本 ----------

def test_rerun_v2_keeps_single_version_and_run_id(settings, store):
    first = score_service.run(settings, store, name="tickets_service_v2.csv")
    second = score_service.run(settings, store, name="tickets_service_v2.csv")
    assert second["replay"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert second["run"]["version"] == first["run"]["version"] == 1
    assert second["score"]["score"] == first["score"]["score"]
    assert store.count_runs() == 1


def test_structural_cells_never_enter_missing_accounts(settings, store):
    result = score_service.run(settings, store,
                               name="tickets_mixed_templates.csv")
    missing = result["missing"]
    # 结构性格不进单元格账，闭口恒等式仍然成立
    account = missing["account"]
    assert account["closed"] is True
    assert account["cells_total"] == account["missing"] + account["filled"]
    # 连续段 / 签名 / 共缺对里都不允许出现 structural_missing
    serialized = str(missing["runs"]) + str(missing["patterns"]) \
        + str(missing["co_missing"])
    assert "structural_missing" not in serialized
    # 真实空值仍被记录：旧段第 4 行 agent、新段第 9 行 service_group
    pattern_cols = {tuple(p["columns"]) for p in missing["patterns"]}
    assert ("agent",) in pattern_cols and ("service_group",) in pattern_cols
    # 画像列级统计里结构性格单列展示，但不计缺失
    sg = next(item for item in result["profile"]["profile"]
              if item["name"] == "service_group")
    assert sg["structural"] == 4 and sg["missing"] == 1


def test_rule_version_unchanged():
    # 模板能力上线不修改规则版本，保证旧库幂等键不漂移、重跑不新增版本
    assert config.RULE_VERSION == "qualiscore/1.0.0"
