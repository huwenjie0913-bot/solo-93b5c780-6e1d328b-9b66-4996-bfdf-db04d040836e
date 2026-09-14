"""HTTP 路由层。"""
from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from . import db as store
from .diff import diff_results
from .grouping import build_groups
from .load_check import check_version_load
from .screening import VERDICT_LABELS, screen_cell, summarize
from .validation import validate_load_check, validate_payload, validate_screening_payload

bp = Blueprint("api", __name__, url_prefix="/api/v1")


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400,
                 fields: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or []


@bp.errorhandler(ApiError)
def handle_api_error(err: ApiError):  # noqa: ANN201
    payload: dict[str, Any] = {
        "error": {"code": err.code, "message": err.message}
    }
    if err.fields:
        payload["error"]["fields"] = err.fields
    return jsonify(payload), err.status


def _db():
    return store.connect(current_app.config["DATABASE"])


def _parse_json() -> dict:
    if not request.is_json:
        raise ApiError("INVALID_CONTENT_TYPE", "Content-Type 必须为 application/json")
    try:
        payload = request.get_json(silent=False)
    except Exception:  # noqa: BLE001
        raise ApiError("INVALID_JSON", "请求体不是合法 JSON")
    if payload is None:
        raise ApiError("INVALID_JSON", "请求体为空")
    return payload


def _validated(payload: dict) -> dict:
    rated = payload.get("rated_capacity_ah")
    cleaned, errors = validate_payload(payload, rated)
    if errors:
        raise ApiError("VALIDATION_FAILED", "入参校验失败，详见 fields", 422, errors)
    return cleaned


def _screening_map(conn, cells: list[dict]) -> dict[str, dict]:
    """查询每只电芯最新筛查批次结论，供配组门禁使用。"""
    rows = store.get_latest_screening_map(conn, [c["cell_id"] for c in cells])
    mapping: dict[str, dict] = {}
    for cell_id, row in rows.items():
        mapping[cell_id] = {
            "verdict": row["verdict"],
            "verdict_label": VERDICT_LABELS.get(row["verdict"], row["verdict"]),
            "batch_id": row["batch_id"],
            "drop_rate_v_per_day": row["drop_rate_v_per_day"],
            "slope_v_per_day": row["slope_v_per_day"],
            "r_squared": row["r_squared"],
            "observation_hours": row["observation_hours"],
            "used_sample_count": row["used_sample_count"],
            "sample_count": row["sample_count"],
            "screened_at": row["created_at"],
        }
    return mapping


def _enforce_flag(payload: dict) -> tuple[bool, list[dict]]:
    """解析可选布尔字段 enforce_screening（默认开启隔离门禁）。"""
    if "enforce_screening" not in payload or payload["enforce_screening"] is None:
        return True, []
    v = payload["enforce_screening"]
    if not isinstance(v, bool):
        return True, [{"field": "enforce_screening",
                       "message": "enforce_screening 必须是布尔值（true/false）"}]
    return v, []


def _version_bundle(plan_row, version_row, result: dict) -> dict:
    return {
        "plan_id": plan_row["id"],
        "plan_name": plan_row["name"],
        "version": version_row["version"],
        "topology": json.loads(plan_row["topology"]),
        "rated_capacity_ah": plan_row["rated_capacity_ah"],
        "thresholds": json.loads(version_row["thresholds"]),
        "note": version_row["note"],
        "created_at": version_row["created_at"],
        "result": result,
    }


@bp.post("/plans")
def create_plan():
    payload = _parse_json()
    data = _validated(payload)
    enforce_screening, enforce_errors = _enforce_flag(payload)
    if enforce_errors:
        raise ApiError("VALIDATION_FAILED", "入参校验失败，详见 fields", 422, enforce_errors)

    with _db() as conn:
        screening_map = _screening_map(conn, data["cells"])
        result = build_groups(
            data["cells"], data["topology"], data["thresholds"],
            data["rated_capacity_ah"],
            screening_map=screening_map,
            enforce_screening=enforce_screening,
        )
        for cell in data["cells"]:
            store.upsert_cell(conn, cell)
        plan_id, version = store.create_plan(
            conn, data["name"], data["topology"],
            data["rated_capacity_ah"], data["thresholds"], result,
            note=payload.get("note"),
        )
        plan_row = store.get_plan(conn, plan_id)
        version_row = store.get_version(conn, plan_id, version)

    return jsonify(_version_bundle(plan_row, version_row, result)), 201


@bp.post("/plans/<int:plan_id>/recompute")
def recompute(plan_id: int):
    payload = _parse_json()
    data = _validated(payload)
    enforce_screening, enforce_errors = _enforce_flag(payload)
    if enforce_errors:
        raise ApiError("VALIDATION_FAILED", "入参校验失败，详见 fields", 422, enforce_errors)
    note = payload.get("note")

    with _db() as conn:
        plan_row = store.get_plan(conn, plan_id)
        if plan_row is None:
            raise ApiError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        topology = json.loads(plan_row["topology"])
        # 拓扑必须与创建时一致；可在请求中省略，省略时沿用原拓扑
        if "topology" in payload and payload["topology"] is not None:
            if (data["topology"]["series"], data["topology"]["parallel"]) != (
                topology["series"], topology["parallel"]
            ):
                raise ApiError(
                    "TOPOLOGY_CONFLICT",
                    f"重算拓扑必须与方案创建时一致（{topology['raw']}）",
                    422,
                    [{"field": "topology", "message": f"期望 {topology['raw']}"}],
                )
        data["topology"] = topology

        screening_map = _screening_map(conn, data["cells"])
        result = build_groups(
            data["cells"], topology, data["thresholds"],
            data["rated_capacity_ah"],
            screening_map=screening_map,
            enforce_screening=enforce_screening,
        )
        for cell in data["cells"]:
            store.upsert_cell(conn, cell)
        version = store.add_version(conn, plan_id, data["thresholds"], result, note=note)
        version_row = store.get_version(conn, plan_id, version)

    return jsonify(_version_bundle(plan_row, version_row, result)), 201


@bp.get("/plans/<int:plan_id>/versions/<int:version>")
def get_version(plan_id: int, version: int):
    with _db() as conn:
        plan_row = store.get_plan(conn, plan_id)
        if plan_row is None:
            raise ApiError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        version_row = store.get_version(conn, plan_id, version)
        if version_row is None:
            raise ApiError("VERSION_NOT_FOUND",
                           f"方案 {plan_id} 不存在版本 {version}", 404)
        result = json.loads(version_row["result_json"])
    return jsonify(_version_bundle(plan_row, version_row, result))


@bp.get("/plans/<int:plan_id>/versions")
def list_versions(plan_id: int):
    with _db() as conn:
        if store.get_plan(conn, plan_id) is None:
            raise ApiError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        rows = store.list_versions(conn, plan_id)
    return jsonify({
        "plan_id": plan_id,
        "versions": [
            {
                "version": r["version"],
                "group_count": r["group_count"],
                "thresholds": json.loads(r["thresholds"]),
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    })


@bp.get("/plans/<int:plan_id>/diff")
def diff(plan_id: int):
    from_version = request.args.get("from", type=int)
    to_version = request.args.get("to", type=int)
    if from_version is None or to_version is None:
        raise ApiError(
            "MISSING_QUERY", "查询参数 from 与 to 必填，例如 ?from=1&to=2",
            fields=[{"field": "from/to", "message": "版本号为正整数"}],
        )

    with _db() as conn:
        if store.get_plan(conn, plan_id) is None:
            raise ApiError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        r_old = store.load_result(conn, plan_id, from_version)
        r_new = store.load_result(conn, plan_id, to_version)
        if r_old is None or r_new is None:
            missing = from_version if r_old is None else to_version
            raise ApiError("VERSION_NOT_FOUND",
                           f"版本 {missing} 不存在", 404,
                           [{"field": "to" if r_old else "from",
                             "message": f"版本 {missing} 不存在"}])

    return jsonify({
        "plan_id": plan_id,
        "from_version": from_version,
        "to_version": to_version,
        "diff": diff_results(r_old, r_new),
    })


@bp.get("/plans/<int:plan_id>/versions/<int:version>/export")
def export_version(plan_id: int, version: int):
    with _db() as conn:
        plan_row = store.get_plan(conn, plan_id)
        if plan_row is None:
            raise ApiError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        version_row = store.get_version(conn, plan_id, version)
        if version_row is None:
            raise ApiError("VERSION_NOT_FOUND",
                           f"方案 {plan_id} 不存在版本 {version}", 404)
        result = json.loads(version_row["result_json"])

    bundle = _version_bundle(plan_row, version_row, result)
    bundle["exported"] = True
    response = jsonify(bundle)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="plan_{plan_id}_v{version}.json"'
    )
    return response


@bp.post("/plans/<int:plan_id>/versions/<int:version>/load-check")
def load_check(plan_id: int, version: int):
    """版本级负载校核：对已保存版本快照逐步计算脉冲放电压降/损耗/容量余量。"""
    payload = _parse_json()
    cleaned, errors = validate_load_check(payload)
    if errors:
        raise ApiError("VALIDATION_FAILED", "入参校验失败，详见 fields", 422, errors)

    with _db() as conn:
        plan_row = store.get_plan(conn, plan_id)
        if plan_row is None:
            raise ApiError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        version_row = store.get_version(conn, plan_id, version)
        if version_row is None:
            raise ApiError("VERSION_NOT_FOUND",
                           f"方案 {plan_id} 不存在版本 {version}", 404)
        version_result = json.loads(version_row["result_json"])

    complete_groups = [g for g in version_result["groups"] if g.get("complete")]
    if not complete_groups:
        raise ApiError(
            "NO_COMPLETE_GROUP",
            f"方案 {plan_id} 版本 {version} 中没有满配成包组，"
            f"仅有尾料/未满配组，无法按完整电池组执行负载校核",
            422,
            [{"field": "version",
              "message": "该版本不存在可构成完整串并联拓扑的电池组"}],
        )

    check = check_version_load(
        version_result,
        cleaned["steps"],
        cleaned["min_terminal_voltage_v"],
        cleaned["max_loss_power_w"],
    )
    return jsonify({
        "plan_id": plan_id,
        "plan_name": plan_row["name"],
        "version": version,
        "topology": json.loads(plan_row["topology"]),
        "load_check": check,
    }), 200


@bp.get("/cells/<cell_id>")
def get_cell(cell_id: str):
    with _db() as conn:
        row = conn.execute("SELECT * FROM cells WHERE cell_id=?", (cell_id,)).fetchone()
    if row is None:
        raise ApiError("CELL_NOT_FOUND", f"电芯 {cell_id} 无档案", 404)
    return jsonify({k: row[k] for k in row.keys()})


# ---------------------------------------------------------------------------
# 静置复测筛查
# ---------------------------------------------------------------------------

def _cell_result_view(row) -> dict[str, Any]:
    """从 result_json 还原逐只筛查结果（已含逐条命中阈值原因与样本明细）。"""
    return json.loads(row["result_json"])


@bp.post("/screenings")
def create_screening():
    """创建静置复测筛查批次：温度修正 → 松弛期剔除 → 速率拟合 → 判定并留存。"""
    payload = _parse_json()
    data, errors = validate_screening_payload(payload)
    if errors:
        raise ApiError("VALIDATION_FAILED", "入参校验失败，详见 fields", 422, errors)

    parameters = data["parameters"]
    cell_results = [
        screen_cell(entry["cell_id"], entry["samples"], parameters)
        for entry in data["cells"]
    ]

    with _db() as conn:
        batch_id, created_at = store.create_screening_batch(
            conn, data["name"], parameters, cell_results
        )
        batch_row = store.get_screening_batch(conn, batch_id)
        cell_rows = store.list_screening_cells(conn, batch_id)
        results = [_cell_result_view(r) for r in cell_rows]

    return jsonify({
        "batch_id": batch_id,
        "name": data["name"],
        "parameters": parameters,
        "created_at": batch_row["created_at"],
        "summary": summarize(results),
        "cells": results,
    }), 201


@bp.get("/screenings")
def list_screenings():
    """筛查批次列表（参数留存、可查询）。"""
    with _db() as conn:
        rows = store.list_screening_batches(conn)
    return jsonify({
        "batches": [
            {
                "batch_id": r["id"],
                "name": r["name"],
                "parameters": json.loads(r["parameters"]),
                "created_at": r["created_at"],
                "cell_count": r["cell_count"] or 0,
                "stable_count": r["stable_count"] or 0,
                "retest_count": r["retest_count"] or 0,
                "quarantined_count": r["quarantined_count"] or 0,
            }
            for r in rows
        ],
    })


@bp.get("/screenings/<int:batch_id>")
def get_screening(batch_id: int):
    """筛查批次详情：参数、逐只电芯结论、拟合结果、逐条命中原因与原始测量。"""
    with _db() as conn:
        batch_row = store.get_screening_batch(conn, batch_id)
        if batch_row is None:
            raise ApiError("SCREENING_BATCH_NOT_FOUND",
                           f"筛查批次 {batch_id} 不存在", 404)
        cell_rows = store.list_screening_cells(conn, batch_id)
        results = [_cell_result_view(r) for r in cell_rows]

    return jsonify({
        "batch_id": batch_id,
        "name": batch_row["name"],
        "parameters": json.loads(batch_row["parameters"]),
        "created_at": batch_row["created_at"],
        "summary": summarize(results),
        "cells": results,
    })


@bp.get("/cells/<cell_id>/screenings")
def list_cell_screenings(cell_id: str):
    """单只电芯的历次筛查结果（按批次倒序）；原始测量随结果返回。"""
    with _db() as conn:
        batch_rows = conn.execute(
            """SELECT sc.batch_id AS batch_id, sc.result_json AS result_json,
                      b.name AS batch_name, b.created_at AS batch_created_at
               FROM screening_cells sc
               JOIN screening_batches b ON b.id = sc.batch_id
               WHERE sc.cell_id = ?
               ORDER BY sc.batch_id DESC""",
            (cell_id,),
        ).fetchall()

    if not batch_rows:
        raise ApiError("SCREENING_NOT_FOUND",
                       f"电芯 {cell_id} 无静置复测筛查记录", 404)
    return jsonify({
        "cell_id": cell_id,
        "screenings": [
            {
                "batch_id": r["batch_id"],
                "batch_name": r["batch_name"],
                "batch_created_at": r["batch_created_at"],
                "result": json.loads(r["result_json"]),
            }
            for r in batch_rows
        ],
    })
