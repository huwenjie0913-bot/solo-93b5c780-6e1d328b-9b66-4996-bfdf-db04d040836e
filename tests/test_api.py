import copy

from conftest import GOOD_BODY, make_cells


def test_create_plan_two_complete_groups(client):
    resp = client.post("/api/v1/plans", json=GOOD_BODY)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["version"] == 1
    result = body["result"]
    assert result["summary"]["total_cells"] == 16
    assert result["summary"]["complete_groups"] == 2
    assert result["summary"]["unassigned_cells"] == 0
    assert result["summary"]["overall_risk_level"] == "LOW"

    g0 = result["groups"][0]
    assert g0["complete"] and g0["cell_count"] == 8
    assert g0["group_no"] == "G001"
    assert g0["metrics"]["usable_capacity_ah"] > 0
    assert g0["weakest_cell"]["cell_id"]
    # 组内电芯按编号应是容量升序排列后的一组
    assert len(g0["cells"]) == 8


def test_tail_group_and_replacement_candidates(client):
    body = copy.deepcopy(GOOD_BODY)
    body["cells"] = make_cells(19, base_cap=100.0, spread=0.01)
    resp = client.post("/api/v1/plans", json=body)
    assert resp.status_code == 201, resp.get_json()
    result = resp.get_json()["result"]
    assert result["summary"]["complete_groups"] == 2
    assert result["summary"]["unassigned_cells"] == 3
    tail = next(g for g in result["groups"] if g["group_no"] == "TAIL")
    assert tail["complete"] is False
    assert tail["risk"]["level"] == "CRITICAL"
    assert any(r["code"] == "INCOMPLETE_GROUP" for r in tail["risk"]["reasons"])


def test_dispersion_triggers_risk_reasons(client):
    body = copy.deepcopy(GOOD_BODY)
    cells = make_cells(8, base_cap=100.0, spread=0.0)
    # 塞入容量与内阻明显异常的电芯
    cells[3]["capacity_ah"] = 80.0
    cells[5]["resistance_mohm"] = 4.5
    body["cells"] = cells
    resp = client.post("/api/v1/plans", json=body)
    assert resp.status_code == 201, resp.get_json()
    group = resp.get_json()["result"]["groups"][0]
    codes = {r["code"] for r in group["risk"]["reasons"]}
    assert "CAPACITY_DISPERSION" in codes or "RESISTANCE_DISPERSION" in codes
    assert group["risk"]["score"] > 0
    assert group["weakest_cell"]["cell_id"] == "C004"
    assert group["metrics"]["usable_capacity_ah"] == 80.0


def test_validation_errors_locate_fields(client):
    body = copy.deepcopy(GOOD_BODY)
    del body["cells"][1]["resistance_mohm"]
    body["cells"][2]["capacity_ah"] = "big"
    body["cells"][3]["ocv_v"] = 99.0
    body["topology"] = "banana"
    body["thresholds"]["capacity_cv_max"] = 5

    resp = client.post("/api/v1/plans", json=body)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "VALIDATION_FAILED"
    fields = {f["field"] for f in err["fields"]}
    assert "cells[1].resistance_mohm" in fields
    assert "cells[2].capacity_ah" in fields
    assert "cells[3].ocv_v" in fields
    assert "topology" in fields
    assert "thresholds.capacity_cv_max" in fields


def test_missing_top_level_fields(client):
    resp = client.post("/api/v1/plans", json={"cells": []})
    assert resp.status_code == 422
    fields = {f["field"] for f in resp.get_json()["error"]["fields"]}
    assert "cells" in fields
    assert "topology" in fields
    assert "rated_capacity_ah" in fields


def test_non_json_body_rejected(client):
    resp = client.post("/api/v1/plans", data="not-json")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "INVALID_CONTENT_TYPE"


def test_duplicate_cell_id(client):
    body = copy.deepcopy(GOOD_BODY)
    body["cells"] = body["cells"][:8]
    body["cells"][5]["cell_id"] = body["cells"][0]["cell_id"]
    resp = client.post("/api/v1/plans", json=body)
    assert resp.status_code == 422
    fields = {f["field"] for f in resp.get_json()["error"]["fields"]}
    assert any(f.startswith("cells[") and f.endswith("cell_id") for f in fields)


def test_recompute_and_version_diff(client):
    r1 = client.post("/api/v1/plans", json=GOOD_BODY)
    plan_id = r1.get_json()["plan_id"]

    # 版本2：收紧容量阈值，人为把容量极差拉大
    body2 = copy.deepcopy(GOOD_BODY)
    body2["thresholds"] = copy.deepcopy(GOOD_BODY["thresholds"])
    body2["thresholds"]["capacity_cv_max"] = 0.0003
    body2["note"] = "收紧容量阈值"
    r2 = client.post(f"/api/v1/plans/{plan_id}/recompute", json=body2)
    assert r2.status_code == 201
    assert r2.get_json()["version"] == 2

    listing = client.get(f"/api/v1/plans/{plan_id}/versions").get_json()
    assert [v["version"] for v in listing["versions"]] == [1, 2]

    diff = client.get(f"/api/v1/plans/{plan_id}/diff?from=1&to=2").get_json()
    assert "threshold_changes" in diff["diff"]
    assert diff["diff"]["threshold_changes"]["capacity_cv_max"]["new"] == 0.0003
    # 阈值收紧后组数或风险必然发生变化
    assert (diff["diff"]["summary_changes"]
            or diff["diff"]["groups"]["changed_groups"]
            or diff["diff"]["groups"]["added_groups"]
            or diff["diff"]["groups"]["removed_groups"])


def test_recompute_topology_conflict(client):
    r1 = client.post("/api/v1/plans", json=GOOD_BODY)
    plan_id = r1.get_json()["plan_id"]
    body2 = copy.deepcopy(GOOD_BODY)
    body2["topology"] = "4S2P"  # 8 只，数量一致但拓扑不同
    resp = client.post(f"/api/v1/plans/{plan_id}/recompute", json=body2)
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "TOPOLOGY_CONFLICT"


def test_get_version_export_and_404(client):
    r1 = client.post("/api/v1/plans", json=GOOD_BODY)
    plan_id = r1.get_json()["plan_id"]

    got = client.get(f"/api/v1/plans/{plan_id}/versions/1")
    assert got.status_code == 200
    assert got.get_json()["result"]["summary"]["total_cells"] == 16

    exported = client.get(f"/api/v1/plans/{plan_id}/versions/1/export")
    assert exported.status_code == 200
    assert exported.headers["Content-Disposition"].startswith("attachment;")
    assert exported.get_json()["exported"] is True

    assert client.get("/api/v1/plans/999/versions/1").status_code == 404
    assert client.get(f"/api/v1/plans/{plan_id}/versions/99").status_code == 404
    assert client.get(f"/api/v1/plans/{plan_id}/diff?from=1&to=99").status_code == 404


def test_parallel_topology_usable_capacity_api(client):
    """回归：4S2P、8 只 100Ah 电芯经完整 API 链路必须返回 200Ah。"""
    body = copy.deepcopy(GOOD_BODY)
    body["topology"] = "4S2P"
    body["cells"] = make_cells(8, base_cap=100.0, spread=0.0)
    resp = client.post("/api/v1/plans", json=body)
    assert resp.status_code == 201, resp.get_json()
    metrics = resp.get_json()["result"]["groups"][0]["metrics"]
    assert metrics["usable_capacity_ah"] == 200.0
    assert metrics["usable_capacity_method"] == "min_parallel_string"
    assert len(metrics["parallel_strings"]) == 4
    assert all(st["capacity_ah"] == 200.0 for st in metrics["parallel_strings"])
    # 保存后重新读取（版本持久化）结果一致
    plan_id = resp.get_json()["plan_id"]
    reloaded = client.get(f"/api/v1/plans/{plan_id}/versions/1").get_json()
    assert reloaded["result"]["groups"][0]["metrics"]["usable_capacity_ah"] == 200.0


def test_cell_archive_persisted(client):
    client.post("/api/v1/plans", json=GOOD_BODY)
    resp = client.get("/api/v1/cells/C001")
    assert resp.status_code == 200
    assert resp.get_json()["cell_id"] == "C001"


def test_health(client):
    assert client.get("/health").status_code == 200
