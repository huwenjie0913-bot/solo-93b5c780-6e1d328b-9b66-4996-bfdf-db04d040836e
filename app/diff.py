"""两个方案版本之间的结构化差异。"""
from __future__ import annotations

from typing import Any

_SUMMARY_KEYS = (
    "total_cells", "complete_groups", "grouped_cells", "unassigned_cells",
    "utilization_pct", "max_risk_score", "mean_risk_score",
    "usable_capacity_per_pack_ah", "weakest_cell_id",
)


def _num_delta(new: float, old: float, ndigits: int = 4) -> dict[str, float]:
    return {"old": old, "new": new, "delta": round(new - old, ndigits)}


def _diff_group_sets(r_old: dict, r_new: dict) -> dict[str, Any]:
    def index_groups(result: dict) -> dict[str, dict]:
        return {g["group_no"]: g for g in result.get("groups", [])}

    g_old = index_groups(r_old)
    g_new = index_groups(r_new)
    old_keys, new_keys = set(g_old), set(g_new)

    changed = []
    for key in sorted(old_keys & new_keys):
        a, b = g_old[key], g_new[key]
        fields: dict[str, Any] = {}
        ma, mb = a["metrics"], b["metrics"]
        for mk in ("capacity_cv", "resistance_cv", "ocv_delta_v", "temperature_delta_c",
                   "usable_capacity_ah", "min_soh"):
            if abs(mb[mk] - ma[mk]) > 1e-9:
                fields[mk] = _num_delta(mb[mk], ma[mk], 6)
        if b["risk"]["score"] != a["risk"]["score"]:
            fields["risk_score"] = _num_delta(b["risk"]["score"], a["risk"]["score"], 2)
        if b["risk"]["level"] != a["risk"]["level"]:
            fields["risk_level"] = {"old": a["risk"]["level"], "new": b["risk"]["level"]}
        if a["complete"] != b["complete"]:
            fields["complete"] = {"old": a["complete"], "new": b["complete"]}
        wa, wb = a["weakest_cell"]["cell_id"], b["weakest_cell"]["cell_id"]
        if wa != wb:
            fields["weakest_cell"] = {"old": wa, "new": wb}
        ids_a = {c["cell_id"] for c in a["cells"]}
        ids_b = {c["cell_id"] for c in b["cells"]}
        members_out = sorted(ids_a - ids_b)
        members_in = sorted(ids_b - ids_a)
        if members_out or members_in:
            fields["membership"] = {"removed": members_out, "added": members_in}
        if fields:
            changed.append({"group_no": key, "changes": fields})

    return {
        "added_groups": sorted(new_keys - old_keys),
        "removed_groups": sorted(old_keys - new_keys),
        "changed_groups": changed,
    }


def diff_results(r_old: dict[str, Any], r_new: dict[str, Any]) -> dict[str, Any]:
    s_old, s_new = r_old["summary"], r_new["summary"]
    summary_changes: dict[str, Any] = {}
    for key in _SUMMARY_KEYS:
        a, b = s_old.get(key), s_new.get(key)
        if a != b:
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                summary_changes[key] = _num_delta(b, a)
            else:
                summary_changes[key] = {"old": a, "new": b}

    if s_old.get("overall_risk_level") != s_new.get("overall_risk_level"):
        summary_changes["overall_risk_level"] = {
            "old": s_old.get("overall_risk_level"),
            "new": s_new.get("overall_risk_level"),
        }

    # 阈值变化
    threshold_changes = {}
    for key in sorted(set(r_old.get("thresholds", {})) | set(r_new.get("thresholds", {}))):
        a = r_old.get("thresholds", {}).get(key)
        b = r_new.get("thresholds", {}).get(key)
        if a != b:
            threshold_changes[key] = {"old": a, "new": b}

    return {
        "summary_changes": summary_changes,
        "threshold_changes": threshold_changes,
        "groups": _diff_group_sets(r_old, r_new),
    }
