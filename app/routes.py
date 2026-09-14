"""HTTP 路由层。"""
from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from . import db as store
from .diff import diff_results
from .grouping import build_groups
from .load_check import check_version_load
from .validation import validate_load_check, validate_payload

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

    result = build_groups(
        data["cells"], data["topology"], data["thresholds"], data["rated_capacity_ah"]
    )
    with _db() as conn:
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

        result = build_groups(
            data["cells"], topology, data["thresholds"],
            data["rated_capacity_ah"],
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
