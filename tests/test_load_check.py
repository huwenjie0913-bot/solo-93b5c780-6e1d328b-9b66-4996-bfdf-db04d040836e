"""版本级负载校核（脉冲放电）端到端测试。

模型：V端 = V开路 − I×R，P损 = I²R，累计 Ah = Σ I·t/3600，
容量余量 = 整包可用容量 − 累计 Ah。尾料/未满配组跳过并说明原因。
"""
import copy

import pytest

from conftest import GOOD_BODY, make_cells


def _create(client, body=None):
    resp = client.post("/api/v1/plans", json=body or GOOD_BODY)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["plan_id"]


def _check(client, plan_id, version, body):
    return client.post(
        f"/api/v1/plans/{plan_id}/versions/{version}/load-check", json=body
    )


# GOOD_BODY: 16 只 8S1P，成两个包；G001 内阻合计 16.14mΩ（C001~C008），
# G002 内阻合计 16.16mΩ；整包 OCV=8×3.2=25.6V；
# G001 可用容量 99.85Ah（最弱 C001），G002 为 100.01Ah。
LIMITS_PASS = {"min_terminal_voltage_v": 24.0, "max_loss_power_w": 100.0}


def test_load_check_passing_scenario(client):
    plan_id = _create(client)
    body = {"steps": [{"current_a": 50.0, "duration_s": 60.0}], **LIMITS_PASS}
    resp = _check(client, plan_id, 1, body)
    assert resp.status_code == 200, resp.get_json()

    data = resp.get_json()
    assert data["plan_id"] == plan_id
    assert data["version"] == 1
    lc = data["load_check"]
    assert lc["complete_group_count"] == 2
    assert lc["excluded_groups"] == []
    assert lc["all_groups_passed"] is True
    assert lc["first_violation"] is None

    g1, g2 = lc["groups"]
    # 50A × 16.14mΩ = 0.807V 压降，端电压 24.793V，损耗 40.35W
    s1 = g1["steps"][0]
    assert s1["step_no"] == 1
    assert s1["voltage_drop_v"] == pytest.approx(0.807, abs=1e-3)
    assert s1["terminal_voltage_v"] == pytest.approx(24.793, abs=1e-3)
    assert s1["loss_power_w"] == pytest.approx(40.35, abs=1e-2)
    assert s1["discharged_ah_step"] == pytest.approx(50 * 60 / 3600, abs=1e-4)
    assert s1["cumulative_ah"] == pytest.approx(0.833333, abs=1e-4)
    assert s1["capacity_margin_ah"] == pytest.approx(99.85 - 0.833333, abs=1e-3)
    assert s1["violated"] is False
    assert s1["violations"] == []

    # 100W 约束下峰值电流 sqrt(100/0.01616)≈78.66A，弱于电压约束 ~99A
    assert g2["peak_current_limited_by"] == "OVER_LOSS_POWER"
    assert g2["sustainable_peak_current_a"] == pytest.approx(78.66, abs=0.05)

    # 最弱组为内阻略大的 G002（16.16mΩ）
    weakest = lc["weakest_group"]
    assert weakest["group_no"] == "G002"
    assert weakest["sustainable_peak_current_a"] == pytest.approx(78.66, abs=0.05)
    assert lc["peak_current_a"] == weakest["sustainable_peak_current_a"]
    assert g1["is_weakest_group"] is False
    assert g2["is_weakest_group"] is True
    assert "内阻" in weakest["reason"]


def test_undervoltage_first_violation_reported(client):
    plan_id = _create(client)
    body = {
        "steps": [{"current_a": 100.0, "duration_s": 10.0}],
        "min_terminal_voltage_v": 25.0,
        "max_loss_power_w": 1000.0,
    }
    resp = _check(client, plan_id, 1, body)
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]

    assert lc["all_groups_passed"] is False
    fv = lc["first_violation"]
    assert fv["step_no"] == 1
    assert fv["group_no"] == "G001"
    codes = {r["code"] for r in fv["reasons"]}
    assert codes == {"UNDER_MIN_TERMINAL_VOLTAGE"}
    reason = fv["reasons"][0]
    assert reason["measured"] == pytest.approx(25.6 - 1.614, abs=1e-3)
    assert reason["threshold"] == 25.0

    g1 = lc["groups"][0]
    assert g1["passed"] is False
    assert g1["first_violation"]["step_no"] == 1
    # 电压约束：(25.6-25)/0.01614 ≈ 37.17A，弱于功率约束
    assert g1["peak_current_limited_by"] == "UNDER_MIN_TERMINAL_VOLTAGE"
    assert g1["sustainable_peak_current_a"] == pytest.approx(37.17, abs=0.05)


def test_over_loss_power_violation_only(client):
    plan_id = _create(client)
    body = {
        "steps": [{"current_a": 100.0, "duration_s": 10.0}],
        "min_terminal_voltage_v": 20.0,   # 端电压 23.99V 不越限
        "max_loss_power_w": 100.0,        # 损耗 ~161W 越限
    }
    resp = _check(client, plan_id, 1, body)
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]

    s1 = lc["groups"][0]["steps"][0]
    assert s1["violated"] is True
    codes = {r["code"] for r in s1["violations"]}
    assert codes == {"OVER_LOSS_POWER"}
    assert s1["terminal_voltage_v"] == pytest.approx(23.986, abs=1e-3)
    assert lc["first_violation"]["reasons"][0]["code"] == "OVER_LOSS_POWER"


def test_cumulative_ah_and_capacity_exceeded(client):
    plan_id = _create(client)
    # G001 可用容量 99.85Ah：50Ah + 50Ah = 100Ah，第二步越限
    body = {
        "steps": [
            {"current_a": 50.0, "duration_s": 3600.0},   # 50Ah
            {"current_a": 60.0, "duration_s": 3000.0},   # 50Ah
        ],
        "min_terminal_voltage_v": 10.0,
        "max_loss_power_w": 100000.0,
    }
    resp = _check(client, plan_id, 1, body)
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]
    g1 = next(g for g in lc["groups"] if g["group_no"] == "G001")

    s1, s2 = g1["steps"]
    assert s1["cumulative_ah"] == pytest.approx(50.0, abs=1e-4)
    assert s1["capacity_margin_ah"] == pytest.approx(49.85, abs=1e-3)
    assert s1["violated"] is False
    assert s2["cumulative_ah"] == pytest.approx(100.0, abs=1e-4)
    assert s2["capacity_margin_ah"] == pytest.approx(-0.15, abs=1e-3)
    assert {r["code"] for r in s2["violations"]} == {"CAPACITY_EXCEEDED"}
    assert g1["first_violation"]["step_no"] == 2
    assert g1["total_discharged_ah"] == pytest.approx(100.0, abs=1e-4)

    # 首个越限点定位到 G001 第二步（G002 可用 100.01Ah，恰好不越限）
    assert lc["first_violation"]["step_no"] == 2
    assert lc["first_violation"]["group_no"] == "G001"
    assert lc["all_groups_passed"] is False


def test_tail_group_excluded_with_reason(client):
    body = copy.deepcopy(GOOD_BODY)
    body["cells"] = make_cells(19, base_cap=100.0, spread=0.01)
    plan_id = _create(client, body)

    resp = _check(client, plan_id, 1,
                  {"steps": [{"current_a": 10.0, "duration_s": 1.0}],
                   "min_terminal_voltage_v": 24.0, "max_loss_power_w": 1000.0})
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]
    assert lc["complete_group_count"] == 2
    assert lc["excluded_groups_count"] == 1
    tail = lc["excluded_groups"][0]
    assert tail["group_no"] == "TAIL"
    assert tail["cell_count"] == 3
    assert tail["complete"] is False
    assert "尾料" in tail["reason"]
    assert "8S1P" in tail["reason"]
    assert all(g["group_no"] != "TAIL" for g in lc["groups"])


def test_no_complete_group_returns_422(client):
    body = copy.deepcopy(GOOD_BODY)
    body["cells"] = make_cells(3, base_cap=100.0, spread=0.0)
    plan_id = _create(client, body)

    resp = _check(client, plan_id, 1,
                  {"steps": [{"current_a": 10.0, "duration_s": 1.0}],
                   "min_terminal_voltage_v": 24.0, "max_loss_power_w": 1000.0})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "NO_COMPLETE_GROUP"


def test_weakest_group_is_high_resistance_pack(client):
    """8 只 2mΩ 成 G001、8 只 3mΩ 成 G002：大内阻包峰值电流更小、最先越限。"""
    cells = []
    for i in range(16):
        cells.append({
            "cell_id": f"C{i + 1:03d}",
            "capacity_ah": 100.0,
            "resistance_mohm": 2.0 if i < 8 else 3.0,
            "ocv_v": 3.2,
            "cycles": 500,
            "temperature_c": 25.0,
        })
    body = copy.deepcopy(GOOD_BODY)
    body["cells"] = cells
    plan_id = _create(client, body)

    resp = _check(client, plan_id, 1,
                  {"steps": [{"current_a": 50.0, "duration_s": 10.0}],
                   "min_terminal_voltage_v": 24.5, "max_loss_power_w": 100000.0})
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]
    weakest = lc["weakest_group"]
    assert weakest["group_no"] == "G002"
    assert weakest["pack_resistance_mohm"] == pytest.approx(24.0, abs=1e-3)
    # (25.6-24.5)/0.024 ≈ 45.83A；G001 ≈ 68A
    assert weakest["sustainable_peak_current_a"] == pytest.approx(45.83, abs=0.05)
    assert weakest["peak_current_limited_by"] == "UNDER_MIN_TERMINAL_VOLTAGE"

    # 50A 下 G002 端电压 24.4V 越限，G001 端电压 24.79V 不越限
    g1, g2 = lc["groups"]
    assert g1["passed"] is True
    assert g2["passed"] is False
    assert lc["first_violation"]["group_no"] == "G002"
    assert lc["first_violation"]["step_no"] == 1


def test_parallel_topology_uses_pack_snapshot(client):
    """4S2P：整包 OCV=12.8V、内阻 4mΩ、可用容量 200Ah（取版本快照）。"""
    body = copy.deepcopy(GOOD_BODY)
    body["topology"] = "4S2P"
    body["cells"] = make_cells(8, base_cap=100.0, spread=0.0, resist=2.0)
    # make_cells 带 i%3 内阻抖动，本用例需要均一 2mΩ
    for c in body["cells"]:
        c["resistance_mohm"] = 2.0
    plan_id = _create(client, body)

    resp = _check(client, plan_id, 1,
                  {"steps": [{"current_a": 100.0, "duration_s": 10.0}],
                   "min_terminal_voltage_v": 12.0, "max_loss_power_w": 1000.0})
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]
    g = lc["groups"][0]
    assert g["pack_ocv_v"] == pytest.approx(12.8, abs=1e-6)
    assert g["pack_resistance_mohm"] == pytest.approx(4.0, abs=1e-6)
    assert g["usable_capacity_ah"] == 200.0
    s = g["steps"][0]
    assert s["voltage_drop_v"] == pytest.approx(0.4, abs=1e-6)
    assert s["terminal_voltage_v"] == pytest.approx(12.4, abs=1e-6)
    assert s["loss_power_w"] == pytest.approx(40.0, abs=1e-3)
    assert s["capacity_margin_ah"] == pytest.approx(200.0 - 100 * 10 / 3600, abs=1e-4)
    assert g["passed"] is True
    # 电压约束 (12.8-12)/0.004=200A，功率约束 sqrt(1000/0.004)=500A
    assert g["sustainable_peak_current_a"] == pytest.approx(200.0, abs=0.01)
    assert g["peak_current_limited_by"] == "UNDER_MIN_TERMINAL_VOLTAGE"


@pytest.mark.parametrize("patch,field", [
    ({"steps": [{"current_a": -1.0, "duration_s": 10.0}]}, "steps[0].current_a"),
    ({"steps": [{"current_a": 10.0, "duration_s": 0.0}]}, "steps[0].duration_s"),
    ({"steps": [{"current_a": 10.0, "duration_s": -5.0}]}, "steps[0].duration_s"),
    ({"steps": [], "min_terminal_voltage_v": 24.0,
      "max_loss_power_w": 100.0}, "steps"),
    ({"steps": [{"current_a": 10.0, "duration_s": 10.0}],
      "min_terminal_voltage_v": 0.0, "max_loss_power_w": 100.0},
     "min_terminal_voltage_v"),
    ({"steps": [{"current_a": 10.0, "duration_s": 10.0}],
      "min_terminal_voltage_v": 24.0, "max_loss_power_w": -1.0},
     "max_loss_power_w"),
    ({"steps": [{"current_a": "x", "duration_s": 10.0}],
      "min_terminal_voltage_v": 24.0, "max_loss_power_w": 100.0},
     "steps[0].current_a"),
    ({"steps": [{"duration_s": 10.0}],
      "min_terminal_voltage_v": 24.0, "max_loss_power_w": 100.0},
     "steps[0].current_a"),
    ({"steps": [{"current_a": 10.0}],
      "min_terminal_voltage_v": 24.0, "max_loss_power_w": 100.0},
     "steps[0].duration_s"),
    ({"min_terminal_voltage_v": 24.0, "max_loss_power_w": 100.0}, "steps"),
    ({"steps": [{"current_a": 10.0, "duration_s": 10.0}],
      "max_loss_power_w": 100.0}, "min_terminal_voltage_v"),
    ({"steps": [{"current_a": 10.0, "duration_s": 10.0}],
      "min_terminal_voltage_v": 24.0}, "max_loss_power_w"),
])
def test_invalid_payload_field_errors(client, patch, field):
    plan_id = _create(client)
    resp = _check(client, plan_id, 1, patch)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "VALIDATION_FAILED"
    fields = {f["field"] for f in err["fields"]}
    assert field in fields


def test_zero_current_allowed(client):
    """静置步骤 I=0 合法：零压降、零损耗、容量余量不变。"""
    plan_id = _create(client)
    resp = _check(client, plan_id, 1,
                  {"steps": [{"current_a": 0.0, "duration_s": 60.0}],
                   "min_terminal_voltage_v": 24.0, "max_loss_power_w": 100.0})
    assert resp.status_code == 200, resp.get_json()
    s = resp.get_json()["load_check"]["groups"][0]["steps"][0]
    assert s["voltage_drop_v"] == 0.0
    assert s["terminal_voltage_v"] == pytest.approx(25.6, abs=1e-6)
    assert s["loss_power_w"] == 0.0
    assert s["cumulative_ah"] == 0.0
    assert s["violated"] is False


def test_voltage_limit_above_ocv_gives_zero_peak_current(client):
    """边界：电压下限 30V 高于整包开路电压 25.6V 时，峰值电流必须是 0A 而非负值。

    即使 I=0（端电压=开路电压）也达不到下限，首个越限点与
    UNDER_MIN_TERMINAL_VOLTAGE 原因仍需保留。
    """
    plan_id = _create(client)
    body = {
        "steps": [{"current_a": 50.0, "duration_s": 10.0}],
        "min_terminal_voltage_v": 30.0,
        "max_loss_power_w": 100000.0,
    }
    resp = _check(client, plan_id, 1, body)
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]

    for g in lc["groups"]:
        assert g["sustainable_peak_current_a"] == 0.0
        assert g["peak_current_limited_by"] == "UNDER_MIN_TERMINAL_VOLTAGE"
        assert g["passed"] is False

    assert lc["peak_current_a"] == 0.0
    assert lc["peak_current_limited_by"] == "UNDER_MIN_TERMINAL_VOLTAGE"
    assert lc["all_groups_passed"] is False
    assert lc["weakest_group"]["sustainable_peak_current_a"] == 0.0

    fv = lc["first_violation"]
    assert fv["step_no"] == 1
    assert {r["code"] for r in fv["reasons"]} == {"UNDER_MIN_TERMINAL_VOLTAGE"}
    # I=0 时端电压本就只有 25.6V；50A 下还要再降 0.807V
    measured = fv["reasons"][0]["measured"]
    assert measured == pytest.approx(25.6 - 50 * 0.01614, abs=1e-3)
    assert fv["reasons"][0]["threshold"] == 30.0


def test_zero_current_step_violates_when_limit_above_ocv(client):
    """零电流静置步骤在 OCV 本身低于下限时也应判越限，且峰值电流为 0A。"""
    plan_id = _create(client)
    body = {
        "steps": [{"current_a": 0.0, "duration_s": 60.0}],
        "min_terminal_voltage_v": 30.0,
        "max_loss_power_w": 100000.0,
    }
    resp = _check(client, plan_id, 1, body)
    assert resp.status_code == 200, resp.get_json()
    lc = resp.get_json()["load_check"]

    g1 = lc["groups"][0]
    s1 = g1["steps"][0]
    assert s1["voltage_drop_v"] == 0.0
    assert s1["terminal_voltage_v"] == pytest.approx(25.6, abs=1e-3)
    assert s1["violated"] is True
    assert {r["code"] for r in s1["violations"]} == {"UNDER_MIN_TERMINAL_VOLTAGE"}
    assert g1["first_violation"]["step_no"] == 1
    assert g1["sustainable_peak_current_a"] == 0.0
    assert g1["passed"] is False
    assert lc["first_violation"]["step_no"] == 1
    assert lc["all_groups_passed"] is False


def test_plan_and_version_not_found(client):
    body = {"steps": [{"current_a": 10.0, "duration_s": 1.0}],
            "min_terminal_voltage_v": 24.0, "max_loss_power_w": 1000.0}
    resp = _check(client, 999, 1, body)
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "PLAN_NOT_FOUND"

    plan_id = _create(client)
    resp = _check(client, plan_id, 99, body)
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_non_json_body_rejected(client):
    plan_id = _create(client)
    resp = client.post(
        f"/api/v1/plans/{plan_id}/versions/1/load-check", data="not-json"
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "INVALID_CONTENT_TYPE"
