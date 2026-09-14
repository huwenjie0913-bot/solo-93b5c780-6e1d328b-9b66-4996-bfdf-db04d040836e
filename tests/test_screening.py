"""静置复测筛查端到端测试。

覆盖：温度修正、松弛期剔除、最小观察跨度、速率拟合/R²、稳定/待复测/隔离
判定与逐条命中原因、批次参数持久化与查询、原始测量不可变、筛查对配组门禁
（默认拒绝隔离、待复测记组级风险）、后续筛查不改写已存版本快照。
"""
import copy
import datetime

import pytest


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------

def _iso(start: datetime.datetime, hours: float) -> str:
    return (start + datetime.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)

DEFAULT_PARAMS = {
    "relaxation_hours": 24.0,
    "min_observation_hours": 72.0,
    "reference_temperature_c": 25.0,
    "temperature_coefficient_v_per_c": 0.001,
    "max_voltage_drop_v_per_day": 0.005,
    "min_r_squared": 0.90,
}


def make_samples(drop_mv_per_day=1.0, n=5, temps=None, gap_hours=24.0,
                 start_offset_h=0.0, ocv0=3.3, jitter_mv=None):
    """构造每 gap_hours 一条、线性下降 drop_mv_per_day 的复测序列。

    第一条落在 start_offset_h（相对 T0），用于测试松弛期。
    jitter_mv: 列表/函数，按天叠加噪声制造低 R²。
    """
    samples = []
    for i in range(n):
        hours = start_offset_h + gap_hours * i
        v = ocv0 - drop_mv_per_day / 1000.0 * (hours / 24.0)
        if jitter_mv is not None:
            j = jitter_mv[i] if isinstance(jitter_mv, (list, tuple)) else jitter_mv(i)
            v += j / 1000.0
        samples.append({
            "sampled_at": _iso(T0, hours),
            "ocv_v": round(v, 6),
            "temperature_c": temps[i] if temps else 25.0,
        })
    return samples


def screening_body(cells, params=None, name="B批静置复测"):
    return {"name": name, "parameters": params or DEFAULT_PARAMS, "cells": cells}


def cell(cell_id, samples):
    return {"cell_id": cell_id, "samples": samples}


def _post_screening(client, cells, params=None, name="B批静置复测"):
    return client.post("/api/v1/screenings",
                       json=screening_body(cells, params, name))


def _by_id(batch):
    return {c["cell_id"]: c for c in batch["cells"]}


# ---------------------------------------------------------------------------
# 判定：稳定 / 隔离 / 待复测
# ---------------------------------------------------------------------------

def test_stable_cell(client):
    r = _post_screening(client, [cell("S1", make_samples(2.0))])
    assert r.status_code == 201, r.get_json()
    d = r.get_json()
    assert d["summary"] == {"total_cells": 1, "stable_count": 1,
                            "retest_count": 0, "quarantined_count": 0}
    s = _by_id(d)["S1"]
    assert s["verdict"] == "STABLE"
    assert s["verdict_label"] == "稳定"
    assert s["reasons"] == []
    fit = s["fit"]
    # 5 条采样、首条（0h）落在 24h 松弛期内被剔除，4 条用于拟合
    assert s["sample_count"] == 5
    assert s["relaxation_excluded_count"] == 1
    assert s["used_sample_count"] == 4
    assert fit["used_sample_count"] == 4
    assert fit["observation_hours"] == 72.0
    # 下降约 2 mV/天；斜率（V/天）为负，drop_rate 为正
    assert fit["drop_rate_v_per_day"] == pytest.approx(0.002, abs=1e-6)
    assert fit["slope_v_per_day"] == pytest.approx(-0.002, abs=1e-6)
    assert fit["r_squared"] == pytest.approx(1.0, abs=1e-6)
    assert not s["samples"][0]["used_in_fit"]
    assert all(x["used_in_fit"] for x in s["samples"][1:])


def test_quarantine_when_drop_rate_exceeds_threshold(client):
    # 10 mV/天 > 5 mV/天阈值
    r = _post_screening(client, [cell("Q1", make_samples(10.0))])
    q = _by_id(r.get_json())["Q1"]
    assert q["verdict"] == "QUARANTINE"
    assert q["verdict_label"] == "隔离"
    assert len(q["reasons"]) == 1
    reason = q["reasons"][0]
    assert reason["code"] == "SELF_DISCHARGE_RATE_EXCEEDED"
    assert reason["measured"] == pytest.approx(0.010, abs=1e-6)
    assert reason["threshold"] == 0.005
    assert "mV/天" in reason["message"]


def test_drop_rate_equal_threshold_is_stable(client):
    """边界：下降速率恰等阈值时不判隔离（> 才隔离）。"""
    r = _post_screening(client, [cell("E1", make_samples(5.0))])
    assert _by_id(r.get_json())["E1"]["verdict"] == "STABLE"


def test_retest_when_observation_span_too_short(client):
    # 3 条采样跨 48h；剔除首条后仅 24h 观察跨度 < 72h
    r = _post_screening(client, [cell("R1", make_samples(10.0, n=3))])
    rr = _by_id(r.get_json())["R1"]
    assert rr["verdict"] == "RETEST"
    assert rr["fit"] is not None
    assert rr["reasons"][0]["code"] == "OBSERVATION_SPAN_TOO_SHORT"
    assert rr["reasons"][0]["measured"] == 24.0
    assert rr["reasons"][0]["threshold"] == 72.0


def test_retest_when_only_one_sample(client):
    r = _post_screening(client, [cell("R2", make_samples(1.0, n=1))])
    rr = _by_id(r.get_json())["R2"]
    assert rr["verdict"] == "RETEST"
    assert rr["fit"] is None
    assert rr["reasons"][0]["code"] == "INSUFFICIENT_SAMPLES"


def test_retest_when_relaxation_leaves_too_few(client):
    # 2 条采样（0h、24h）：首条在松弛期内被剔除，仅剩 1 条
    r = _post_screening(client, [cell("R3", make_samples(1.0, n=2))])
    rr = _by_id(r.get_json())["R3"]
    assert rr["verdict"] == "RETEST"
    assert rr["reasons"][0]["code"] == "RELAXATION_LEFT_TOO_FEW"
    assert rr["relaxation_excluded_count"] == 1
    assert rr["used_sample_count"] == 1


def test_retest_when_poor_fit_quality(client):
    # 大噪声导致线性拟合 R² 很低；观察跨度足够，速率其实很大但先卡在拟合质量
    jitter = [0.0, 40.0, -35.0, 40.0, -35.0]
    r = _post_screening(client, [cell("R4", make_samples(1.0, jitter_mv=jitter))])
    rr = _by_id(r.get_json())["R4"]
    assert rr["verdict"] == "RETEST"
    assert rr["reasons"][0]["code"] == "POOR_FIT_QUALITY"
    assert rr["fit"]["r_squared"] < 0.9
    assert rr["reasons"][0]["measured"] == rr["fit"]["r_squared"]
    assert rr["reasons"][0]["threshold"] == 0.9


def test_batch_summary_counts(client):
    cells = [
        cell("S1", make_samples(1.0)),
        cell("S2", make_samples(2.0)),
        cell("Q1", make_samples(20.0)),
        cell("R1", make_samples(1.0, n=2)),
    ]
    r = _post_screening(client, cells)
    assert r.get_json()["summary"] == {"total_cells": 4, "stable_count": 2,
                                       "retest_count": 1, "quarantined_count": 1}


# ---------------------------------------------------------------------------
# 温度补偿
# ---------------------------------------------------------------------------

def test_ocv_corrected_to_reference_temperature(client):
    # α=0.001 V/°C：30°C 实测 3.300V → 修正到 25°C 为 3.295V
    samples = make_samples(0.0, n=5, temps=[30.0, 30.0, 20.0, 25.0, 25.0])
    r = _post_screening(client, [cell("T1", samples)])
    s = _by_id(r.get_json())["T1"]
    corrected = [x["ocv_corrected_v"] for x in s["samples"]]
    assert corrected[0] == pytest.approx(3.295, abs=1e-6)
    assert corrected[1] == pytest.approx(3.295, abs=1e-6)
    # 20°C：修正到 25°C 为 3.305V
    assert corrected[2] == pytest.approx(3.305, abs=1e-6)
    assert corrected[3] == pytest.approx(3.300, abs=1e-6)


def test_temperature_compensation_reveals_real_self_discharge(client):
    """实测电压看似在涨，但都是温度升高造成；修正到参考温度后暴露自放电。"""
    # 每天实测 OCV 上升 5mV，但温度同步升高 10°C（α=0.001 → 修正减去 10mV），
    # 修正后实际每天下降 5mV 边界，取 6mV 实测上升 + 12°C 升温 → 修正 -6mV/天
    temps = [25.0, 37.0, 49.0]
    raw = [3.300, 3.306, 3.312]
    samples = []
    for i, (v, t) in enumerate(zip(raw, temps)):
        samples.append({"sampled_at": _iso(T0, 48.0 + 24 * i),  # 跳过松弛期
                        "ocv_v": v, "temperature_c": t})
    params = {**DEFAULT_PARAMS, "relaxation_hours": 24.0,
              "min_observation_hours": 24.0,
              "temperature_coefficient_v_per_c": 0.001}
    r = _post_screening(client, [cell("T2", samples)], params=params)
    s = _by_id(r.get_json())["T2"]
    # 修正值：3.300+(25-t)*0.001 → 3.300, 3.294, 3.288：每天下降 6mV
    assert s["verdict"] == "QUARANTINE"
    assert s["fit"]["drop_rate_v_per_day"] == pytest.approx(0.006, abs=1e-6)


def test_negative_temperature_coefficient_accepted(client):
    params = {**DEFAULT_PARAMS, "temperature_coefficient_v_per_c": -0.0005}
    r = _post_screening(client, [cell("N1", make_samples(1.0))], params=params)
    assert r.status_code == 201
    assert r.get_json()["parameters"]["temperature_coefficient_v_per_c"] == -0.0005


# ---------------------------------------------------------------------------
# 松弛期与参数可配置
# ---------------------------------------------------------------------------

def test_custom_relaxation_period(client):
    # 0h、24h、48h、72h、96h；松弛期设为 48h → 前两条剔除
    params = {**DEFAULT_PARAMS, "relaxation_hours": 48.0}
    r = _post_screening(client, [cell("X1", make_samples(1.0))], params=params)
    s = _by_id(r.get_json())["X1"]
    assert s["relaxation_excluded_count"] == 2
    assert s["used_sample_count"] == 3
    used = [x for x in s["samples"] if x["used_in_fit"]]
    assert used[0]["elapsed_hours"] == 48.0
    assert s["fit"]["observation_hours"] == 48.0


def test_zero_relaxation_keeps_origin_sample(client):
    params = {**DEFAULT_PARAMS, "relaxation_hours": 0.0,
              "min_observation_hours": 24.0}
    samples = make_samples(1.0, n=3)  # 0h,24h,48h
    r = _post_screening(client, [cell("X2", samples)], params=params)
    s = _by_id(r.get_json())["X2"]
    assert s["relaxation_excluded_count"] == 0
    assert s["used_sample_count"] == 3
    assert s["fit"]["observation_hours"] == 48.0
    assert s["verdict"] == "STABLE"


def test_custom_thresholds_take_effect(client):
    # 2 mV/天；阈值放宽到 3 mV/天 → 稳定
    params = {**DEFAULT_PARAMS, "max_voltage_drop_v_per_day": 0.003}
    r = _post_screening(client, [cell("P1", make_samples(2.0))], params=params)
    assert _by_id(r.get_json())["P1"]["verdict"] == "STABLE"
    # 默认阈值 5 mV/天 下同样电芯本来也稳定；改成 1 mV/天 → 隔离
    params["max_voltage_drop_v_per_day"] = 0.001
    r = _post_screening(client, [cell("P1", make_samples(2.0))], params=params)
    assert _by_id(r.get_json())["P1"]["verdict"] == "QUARANTINE"


def test_default_parameters_when_omitted(client):
    body = {"cells": [cell("D1", make_samples(1.0))]}
    r = client.post("/api/v1/screenings", json=body)
    assert r.status_code == 201, r.get_json()
    d = r.get_json()
    for key, value in DEFAULT_PARAMS.items():
        assert d["parameters"][key] == value


# ---------------------------------------------------------------------------
# 持久化 / 查询 / 不可变
# ---------------------------------------------------------------------------

def test_batch_persisted_and_queryable(client):
    cells = [cell("S1", make_samples(1.0)), cell("Q1", make_samples(20.0))]
    created = _post_screening(client, cells).get_json()
    bid = created["batch_id"]

    got = client.get(f"/api/v1/screenings/{bid}")
    assert got.status_code == 200
    d = got.get_json()
    assert d["batch_id"] == bid
    assert d["parameters"] == DEFAULT_PARAMS
    assert d["summary"]["quarantined_count"] == 1
    # 原始测量随结果留存
    s1 = _by_id(d)["S1"]
    assert len(s1["samples"]) == 5
    assert s1["samples"][0]["ocv_v"] == pytest.approx(3.3, abs=1e-9)
    assert {"seq", "sampled_at", "elapsed_hours", "ocv_v", "temperature_c",
            "ocv_corrected_v", "used_in_fit"} <= set(s1["samples"][0])


def test_batch_listing(client):
    _post_screening(client, [cell("S1", make_samples(1.0))], name="第一批")
    _post_screening(client, [cell("Q1", make_samples(20.0)),
                             cell("S2", make_samples(1.0))],
                    name="第二批")
    listing = client.get("/api/v1/screenings").get_json()["batches"]
    assert [b["batch_id"] for b in listing] == [1, 2]
    assert listing[0]["name"] == "第一批"
    assert listing[1]["cell_count"] == 2
    assert listing[1]["quarantined_count"] == 1
    assert listing[1]["stable_count"] == 1
    assert listing[1]["retest_count"] == 0


def test_cell_screening_history_latest_first(client):
    # 第一次稳定
    _post_screening(client, [cell("H1", make_samples(1.0))], name="初筛")
    # 第二次隔离
    _post_screening(client, [cell("H1", make_samples(20.0))], name="复筛")

    r = client.get("/api/v1/cells/H1/screenings")
    assert r.status_code == 200
    items = r.get_json()["screenings"]
    assert [x["batch_id"] for x in items] == [2, 1]
    assert items[0]["result"]["verdict"] == "QUARANTINE"
    assert items[1]["result"]["verdict"] == "STABLE"
    assert items[0]["batch_name"] == "复筛"


def test_original_measurements_not_modified_by_later_screening(client):
    """后续筛查只追加新批次，不改写此前批次的原始测量与结论。"""
    first = _post_screening(client, [cell("M1", make_samples(1.0))],
                            name="第一次").get_json()
    bid1 = first["batch_id"]
    before = client.get(f"/api/v1/screenings/{bid1}").get_json()

    # 用完全不同的样本/参数再筛一次
    other_params = {**DEFAULT_PARAMS, "max_voltage_drop_v_per_day": 0.001}
    _post_screening(client, [cell("M1", make_samples(30.0, n=4))],
                    params=other_params, name="第二次")

    after = client.get(f"/api/v1/screenings/{bid1}").get_json()
    assert after == before
    assert _by_id(after)["M1"]["verdict"] == "STABLE"
    assert len(_by_id(after)["M1"]["samples"]) == 5


def test_screening_batch_not_found(client):
    r = client.get("/api/v1/screenings/999")
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "SCREENING_BATCH_NOT_FOUND"


def test_cell_screening_not_found(client):
    r = client.get("/api/v1/cells/NOPE/screenings")
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "SCREENING_NOT_FOUND"


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch,field", [
    ({"cells": []}, "cells"),
    ({"cells": [{"cell_id": "Z1"}]}, "cells[0].samples"),
    ({"cells": [{"samples": [{"sampled_at": _iso(T0, 0),
                              "ocv_v": 3.3, "temperature_c": 25}]}]},
     "cells[0].cell_id"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"ocv_v": 3.3, "temperature_c": 25}]}]}, "cells[0].samples[0].sampled_at"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": "not-a-time", "ocv_v": 3.3, "temperature_c": 25}]}]},
     "cells[0].samples[0].sampled_at"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": _iso(T0, 0), "temperature_c": 25}]}]},
     "cells[0].samples[0].ocv_v"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": _iso(T0, 0), "ocv_v": 9.0, "temperature_c": 25}]}]},
     "cells[0].samples[0].ocv_v"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": _iso(T0, 0), "ocv_v": 3.3}]}]},
     "cells[0].samples[0].temperature_c"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": _iso(T0, 0), "ocv_v": 3.3, "temperature_c": 200}]}]},
     "cells[0].samples[0].temperature_c"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": _iso(T0, 24), "ocv_v": 3.3, "temperature_c": 25},
        {"sampled_at": _iso(T0, 0), "ocv_v": 3.3, "temperature_c": 25}]}]},
     "cells[0].samples[1].sampled_at"),
    ({"cells": [{"cell_id": "Z1", "samples": [
        {"sampled_at": _iso(T0, 0), "ocv_v": 3.3, "temperature_c": 25},
        {"sampled_at": _iso(T0, 0), "ocv_v": 3.29, "temperature_c": 25}]}]},
     "cells[0].samples[1].sampled_at"),
])
def test_invalid_samples_field_errors(client, patch, field):
    body = {"parameters": DEFAULT_PARAMS, **patch}
    r = client.post("/api/v1/screenings", json=body)
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert err["code"] == "VALIDATION_FAILED"
    assert field in {f["field"] for f in err["fields"]}


def test_duplicate_cell_id_in_screening_batch(client):
    body = screening_body([cell("DUP", make_samples(1.0)),
                           cell("DUP", make_samples(2.0))])
    r = client.post("/api/v1/screenings", json=body)
    assert r.status_code == 422
    fields = {f["field"] for f in r.get_json()["error"]["fields"]}
    assert any(f.endswith("cell_id") for f in fields)


@pytest.mark.parametrize("key,bad_value", [
    ("relaxation_hours", -1.0),
    ("min_observation_hours", -5.0),
    ("reference_temperature_c", 200.0),
    ("temperature_coefficient_v_per_c", 1.0),
    ("max_voltage_drop_v_per_day", -0.1),
    ("min_r_squared", 1.5),
    ("min_r_squared", "x"),
])
def test_invalid_parameters_field_errors(client, key, bad_value):
    params = {**DEFAULT_PARAMS, key: bad_value}
    r = _post_screening(client, [cell("Z1", make_samples(1.0))], params)
    assert r.status_code == 422
    fields = {f["field"] for f in r.get_json()["error"]["fields"]}
    assert f"parameters.{key}" in fields


def test_non_json_body_rejected(client):
    r = client.post("/api/v1/screenings", data="not-json")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "INVALID_CONTENT_TYPE"


def test_naive_timestamp_treated_as_utc(client):
    ts = "2026-09-01T00:00:00"  # 无时区，按 UTC
    samples = [
        {"sampled_at": ts, "ocv_v": 3.3, "temperature_c": 25.0},
        {"sampled_at": "2026-09-02T00:00:00Z", "ocv_v": 3.299, "temperature_c": 25.0},
        {"sampled_at": "2026-09-03T00:00:00Z", "ocv_v": 3.298, "temperature_c": 25.0},
        {"sampled_at": "2026-09-04T00:00:00Z", "ocv_v": 3.297, "temperature_c": 25.0},
        {"sampled_at": "2026-09-05T00:00:00Z", "ocv_v": 3.296, "temperature_c": 25.0},
    ]
    r = _post_screening(client, [cell("U1", samples)])
    assert r.status_code == 201
    s = _by_id(r.get_json())["U1"]
    assert s["samples"][0]["used_in_fit"] is False
    assert s["used_sample_count"] == 4


# ---------------------------------------------------------------------------
# 与配组门禁集成
# ---------------------------------------------------------------------------

PLAN_CELLS = [
    {"cell_id": f"C{i + 1:03d}", "capacity_ah": 100.0, "resistance_mohm": 2.0,
     "ocv_v": 3.2, "cycles": 500, "temperature_c": 25.0}
    for i in range(16)
]

PLAN_BODY = {
    "name": "配组",
    "rated_capacity_ah": 105.0,
    "topology": "8S1P",
    "thresholds": {"capacity_cv_max": 0.05, "resistance_cv_max": 0.10,
                   "temperature_delta_max": 5.0, "ocv_delta_max": 0.05,
                   "soh_min": 0.80},
    "cells": copy.deepcopy(PLAN_CELLS),
}


def _plan(client, body=None):
    return client.post("/api/v1/plans", json=body or copy.deepcopy(PLAN_BODY))


def _plan_result(client, body=None):
    return _plan(client, body).get_json()["result"]


def test_quarantined_cell_rejected_from_plan_by_default(client):
    # 16 只：C003 隔离，其余稳定
    cells = [
        cell(f"C{i + 1:03d}", make_samples(20.0 if i == 2 else 1.0))
        for i in range(16)
    ]
    scr = _post_screening(client, cells).get_json()
    assert _by_id(scr)["C003"]["verdict"] == "QUARANTINE"

    r = _plan(client)
    assert r.status_code == 201
    result = r.get_json()["result"]
    gate = result["screening_gate"]
    assert gate["enforced"] is True
    assert gate["rejected_quarantined_cell_ids"] == ["C003"]
    assert gate["rejected_count"] == 1
    assert result["summary"]["submitted_cells"] == 16
    assert result["summary"]["total_cells"] == 15
    assert result["summary"]["screening_rejected_count"] == 1
    # C003 不出现在任何组
    grouped_ids = {c["cell_id"] for g in result["groups"] for c in g["cells"]}
    assert "C003" not in grouped_ids
    # 门禁明细带最新批次的拟合指标
    rejected = gate["rejected_quarantined"][0]
    assert rejected["cell_id"] == "C003"
    assert rejected["screening"]["verdict"] == "QUARANTINE"
    assert rejected["screening"]["batch_id"] == scr["batch_id"]
    assert rejected["screening"]["drop_rate_v_per_day"] == pytest.approx(0.02, abs=1e-6)


def test_latest_screening_verdict_wins(client):
    # C001 先稳定后隔离：创建方案时必须按最新结论拒绝
    _post_screening(client, [cell("C001", make_samples(1.0))])
    _post_screening(client, [cell("C001", make_samples(20.0))])
    # 其余 15 只在一个批次里筛成稳定
    others = [cell(f"C{i + 1:03d}", make_samples(1.0)) for i in range(1, 16)]
    _post_screening(client, others)

    result = _plan(client).get_json()["result"]
    assert result["screening_gate"]["rejected_quarantined_cell_ids"] == ["C001"]


def test_retest_cell_grouped_but_recorded_as_group_risk(client):
    # 16 只全部成两个满配组；C010 待复测（数据短），其余稳定
    screen_cells = []
    for i in range(16):
        screen_cells.append(
            cell(f"C{i + 1:03d}", make_samples(1.0, n=3) if i == 9 else make_samples(1.0))
        )
    _post_screening(client, screen_cells)
    result = _plan_result(client)

    gate = result["screening_gate"]
    assert gate["rejected_count"] == 0
    assert gate["retest_cell_ids"] == ["C010"]
    assert result["summary"]["screening_retest_count"] == 1
    assert result["summary"]["screening_retest_grouped_count"] == 1

    g_with = next(g for g in result["groups"]
                  if any(c["cell_id"] == "C010" for c in g["cells"]))
    assert g_with["complete"] is True
    assert g_with["metrics"]["screening_retest_count"] == 1
    reason = next(x for x in g_with["risk"]["reasons"]
                  if x["code"] == "SCREENING_RETEST_PENDING")
    assert reason["cell_ids"] == ["C010"]
    assert "待复测" in reason["message"]
    # 待复测为非阻断：仍有两个满配组
    assert result["summary"]["complete_groups"] == 2
    # 电芯视图带筛查结论快照
    c010 = next(c for c in g_with["cells"] if c["cell_id"] == "C010")
    assert c010["screening"]["verdict"] == "RETEST"


def test_stable_and_unscreened_cells_pass_gate(client):
    # 只筛 8 只（稳定），另外 8 只从未筛查
    _post_screening(client, [cell(f"C{i + 1:03d}", make_samples(1.0))
                             for i in range(8)])
    result = _plan_result(client)
    gate = result["screening_gate"]
    assert gate["rejected_count"] == 0
    assert set(gate["stable_cell_ids"]) == {f"C{i + 1:03d}" for i in range(8)}
    assert set(gate["unscreened_cell_ids"]) == {f"C{i + 1:03d}" for i in range(8, 16)}
    assert result["summary"]["screening_unscreened_count"] == 8
    assert result["summary"]["complete_groups"] == 2


def test_enforce_screening_false_warns_instead_of_rejecting(client):
    cells = [cell(f"C{i + 1:03d}", make_samples(20.0 if i == 2 else 1.0))
             for i in range(16)]
    _post_screening(client, cells)

    body = copy.deepcopy(PLAN_BODY)
    body["enforce_screening"] = False
    result = client.post("/api/v1/plans", json=body).get_json()["result"]
    gate = result["screening_gate"]
    assert gate["enforced"] is False
    assert gate["rejected_count"] == 0
    assert gate["warned_quarantined_cell_ids"] == ["C003"]
    # 隔离电芯被放行，但组风险兜底为 CRITICAL
    grouped_ids = {c["cell_id"] for g in result["groups"] for c in g["cells"]}
    assert "C003" in grouped_ids
    g_with = next(g for g in result["groups"]
                  if any(c["cell_id"] == "C003" for c in g["cells"]))
    assert any(x["code"] == "SCREENING_QUARANTINED_CELL"
               for x in g_with["risk"]["reasons"])
    assert g_with["risk"]["level"] == "CRITICAL"


def test_invalid_enforce_screening_type(client):
    body = copy.deepcopy(PLAN_BODY)
    body["enforce_screening"] = "yes"
    r = client.post("/api/v1/plans", json=body)
    assert r.status_code == 422
    fields = {f["field"] for f in r.get_json()["error"]["fields"]}
    assert "enforce_screening" in fields


def test_screening_gate_persisted_in_version_snapshot(client):
    _post_screening(client, [cell("C003", make_samples(20.0))])
    created = _plan(client).get_json()
    plan_id = created["plan_id"]
    reloaded = client.get(f"/api/v1/plans/{plan_id}/versions/1").get_json()
    assert (reloaded["result"]["screening_gate"]["rejected_quarantined_cell_ids"]
            == ["C003"])
    # 版本快照内逐只电芯筛查结论也固化
    all_cells = [c for g in reloaded["result"]["groups"]
                 for c in g["cells"]]
    assert all("screening" in c for c in all_cells)


def test_later_screening_does_not_rewrite_existing_version(client):
    # 初版：无筛查历史 → 16 只全部入组
    v1 = _plan(client).get_json()
    plan_id = v1["plan_id"]
    assert v1["result"]["summary"]["complete_groups"] == 2
    assert v1["result"]["screening_gate"]["rejected_count"] == 0

    # 之后做筛查：C001、C003 隔离
    _post_screening(client, [
        cell(f"C{i + 1:03d}", make_samples(20.0 if i in (0, 2) else 1.0))
        for i in range(16)
    ])

    # 已存版本 1 快照不变（后续筛查不得改写已有方案版本）
    snap = client.get(f"/api/v1/plans/{plan_id}/versions/1").get_json()["result"]
    assert snap["summary"]["complete_groups"] == 2
    assert snap["screening_gate"]["rejected_count"] == 0
    grouped_ids = {c["cell_id"] for g in snap["groups"] for c in g["cells"]}
    assert {"C001", "C003"} <= grouped_ids

    # 重算生成版本 2，应用最新筛查
    body = copy.deepcopy(PLAN_BODY)
    body["note"] = "筛查后重算"
    v2 = client.post(f"/api/v1/plans/{plan_id}/recompute", json=body).get_json()
    assert v2["version"] == 2
    assert set(v2["result"]["screening_gate"]
               ["rejected_quarantined_cell_ids"]) == {"C001", "C003"}

    # 版本 1 依旧不变；两版差异可见门禁带来的成员变化
    snap_again = client.get(f"/api/v1/plans/{plan_id}/versions/1").get_json()["result"]
    assert snap_again["screening_gate"]["rejected_count"] == 0
    listing = client.get(f"/api/v1/plans/{plan_id}/versions").get_json()
    assert [v["version"] for v in listing["versions"]] == [1, 2]


def test_screening_does_not_require_cell_archive(client):
    """筛查是配组前置工序：电芯未在 cells 档案库也能筛。"""
    r = _post_screening(client, [cell("NEW-CELL", make_samples(1.0))])
    assert r.status_code == 201
    assert client.get("/api/v1/cells/NEW-CELL").status_code == 404


def test_recompute_applies_latest_quarantine_and_keeps_versions(client):
    # 先筛：C005 隔离，创建版本 1（拒绝 C005）
    _post_screening(client, [
        cell(f"C{i + 1:03d}", make_samples(20.0 if i == 4 else 1.0))
        for i in range(16)
    ])
    v1 = _plan(client).get_json()
    plan_id = v1["plan_id"]
    assert v1["result"]["screening_gate"]["rejected_quarantined_cell_ids"] == ["C005"]

    # 新一轮筛查：C005 恢复稳定，C006 隔离
    _post_screening(client, [
        cell(f"C{i + 1:03d}", make_samples(20.0 if i == 5 else 1.0))
        for i in range(16)
    ])
    body = copy.deepcopy(PLAN_BODY)
    v2 = client.post(f"/api/v1/plans/{plan_id}/recompute", json=body).get_json()
    assert v2["result"]["screening_gate"]["rejected_quarantined_cell_ids"] == ["C006"]

    # 版本 1 仍记录 C005
    snap1 = client.get(f"/api/v1/plans/{plan_id}/versions/1").get_json()["result"]
    assert snap1["screening_gate"]["rejected_quarantined_cell_ids"] == ["C005"]
