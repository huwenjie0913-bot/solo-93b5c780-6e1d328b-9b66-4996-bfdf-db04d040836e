"""配组核心算法：贪心装箱 + 组内均衡评估 + 风险/候选计算。"""
from __future__ import annotations

import math
from typing import Any

from .validation import DEFAULT_THRESHOLDS
from .screening import QUARANTINE, RETEST


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def pstdev(values: list[float], mu: float | None = None) -> float:
    """总体标准差（与 numpy.std 默认 ddof=0 一致）。"""
    if not values:
        return 0.0
    mu = mean(values) if mu is None else mu
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def cv(values: list[float]) -> float:
    """变异系数 CV = σ/μ；均值为 0 时返回 0。"""
    if not values:
        return 0.0
    mu = mean(values)
    if mu == 0:
        return 0.0
    return pstdev(values, mu) / mu


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _arrange_parallel_strings(
    members: list[dict], s: int, p: int
) -> list[dict[str, Any]]:
    """把电芯分配到 s 个并联串（每串至多 p 只），尽量拉平各串容量。

    采用 LPT（longest-processing-time）贪心：容量从大到小，逐只放入
    当前容量和最小且未满的串，使最弱串（决定整包可用容量）尽量大。
    同容量时按电芯编号打破平局，保证结果确定。
    """
    strings: list[dict[str, Any]] = [
        {"string_no": k + 1, "cells": [], "capacity_ah": 0.0,
         "conductance": 0.0}
        for k in range(s)
    ]
    for cell in sorted(members, key=lambda c: (-c["capacity_ah"], c["cell_id"])):
        idx = min(
            (k for k in range(s) if len(strings[k]["cells"]) < p),
            key=lambda k: (strings[k]["capacity_ah"], k),
        )
        bucket = strings[idx]
        bucket["cells"].append(cell)
        bucket["capacity_ah"] += cell["capacity_ah"]
        bucket["conductance"] += 1.0 / cell["resistance_mohm"]
    return strings


def _string_view(strings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对外的并联串明细（含容量和与并联内阻）。"""
    view = []
    for b in strings:
        resistance = (1.0 / b["conductance"]) if b["conductance"] else None
        view.append({
            "string_no": b["string_no"],
            "cell_ids": [c["cell_id"] for c in b["cells"]],
            "cell_count": len(b["cells"]),
            "capacity_ah": round(b["capacity_ah"], 4),
            "resistance_mohm": round(resistance, 6) if resistance is not None else None,
        })
    return view


def _pack_layout(members: list[dict], topology: dict[str, Any]) -> dict[str, Any]:
    """按串并联拓扑计算成包容量/内阻。

    - 每只并联串容量 = 串内 p 只电芯容量之和，内阻 = p 只内阻并联；
    - s 串串联，整包可用容量受最弱串限制（串联回路各串放出的容量一致）；
    - 整包内阻 = 各串内阻之和；
    - 组未满配时无法构成完整拓扑，退回“最弱单体 + 均值估算”的保守口径。
    """
    s, p = topology["series"], topology["parallel"]
    group_size = topology["group_size"]
    n = len(members)

    # 尾料池电芯数可能超过一组：无法布成 s*p，按未成包保守口径处理
    if n > group_size:
        return {
            "usable_capacity_ah": round(min(c["capacity_ah"] for c in members), 4),
            "pack_resistance_mohm": round(mean([c["resistance_mohm"] for c in members])
                                          / p * s, 6),
            "parallel_strings": [],
            "weakest_string_no": None,
            "string_capacity_cv": None,
            "string_imbalance_pct": 0.0,
            "method": "unassigned_pool",
        }

    strings = _arrange_parallel_strings(members, s, p)
    complete = n == group_size

    if complete:
        sums = [b["capacity_ah"] for b in strings]
        weakest_no = min(range(s), key=lambda k: (sums[k], k)) + 1
        # 串联：各串放出容量一致，整包容量 = 最弱串容量
        usable = min(sums)
        # 各串并联后再串联：内阻相加
        pack_resistance = sum(1.0 / b["conductance"] for b in strings)
        mu = mean(sums)
        imbalance = (max(sums) - min(sums)) / mu if mu else 0.0
        return {
            "usable_capacity_ah": round(usable, 4),
            "pack_resistance_mohm": round(pack_resistance, 6),
            "parallel_strings": _string_view(strings),
            "weakest_string_no": weakest_no,
            "string_capacity_cv": round(cv(sums), 6),
            "string_imbalance_pct": round(imbalance * 100, 4),
            "method": "min_parallel_string",
        }

    # 未满配：保守按最弱单体给可用容量，内阻按组均值做拓扑估算
    nonempty = [b for b in strings if b["cells"]]
    sums = [b["capacity_ah"] for b in nonempty]
    imbalance = 0.0
    if p > 1 and sums:
        mu = mean(sums)
        imbalance = (max(sums) - min(sums)) / mu if mu else 0.0
    return {
        "usable_capacity_ah": round(min(c["capacity_ah"] for c in members), 4),
        "pack_resistance_mohm": round(
            mean([c["resistance_mohm"] for c in members]) / p * s, 6),
        "parallel_strings": _string_view(strings),
        "weakest_string_no": None,
        "string_capacity_cv": round(cv(sums), 6) if sums else None,
        "string_imbalance_pct": round(imbalance * 100, 4),
        "method": "cell_min_shortfall",
    }


def _replacement_candidates(
    members: list[dict],
    leftovers: list[dict],
    thresholds: dict[str, float],
    cap_mu: float,
    res_mu: float,
    limit: int = 5,
) -> list[dict]:
    """从未入组电芯中挑选可替换候选：换入后容量/内阻 CV 仍不越限。"""
    caps = [c["capacity_ah"] for c in members]
    ress = [c["resistance_mohm"] for c in members]
    n = len(members)

    candidates: list[dict] = []
    for cand in leftovers:
        new_caps = caps + [cand["capacity_ah"]]
        new_ress = ress + [cand["resistance_mohm"]]
        cap_cv_after = cv(new_caps)
        res_cv_after = cv(new_ress)
        feasible = (
            cap_cv_after <= thresholds["capacity_cv_max"]
            and res_cv_after <= thresholds["resistance_cv_max"]
        )
        if not feasible:
            continue
        # 与组中心的归一化距离（容量按组均值归一，内阻按组均值归一）
        distance = math.sqrt(
            ((cand["capacity_ah"] - cap_mu) / cap_mu) ** 2
            + ((cand["resistance_mohm"] - res_mu) / res_mu) ** 2
        )
        candidates.append(
            {
                "cell_id": cand["cell_id"],
                "capacity_ah": cand["capacity_ah"],
                "resistance_mohm": cand["resistance_mohm"],
                "ocv_v": cand["ocv_v"],
                "cycles": cand["cycles"],
                "temperature_c": cand["temperature_c"],
                "soh": cand["soh"],
                "capacity_delta_ah": round(cand["capacity_ah"] - cap_mu, 4),
                "resistance_delta_mohm": round(cand["resistance_mohm"] - res_mu, 4),
                "projected_capacity_cv": round(cap_cv_after, 6),
                "projected_resistance_cv": round(res_cv_after, 6),
                "distance": round(distance, 6),
            }
        )
    candidates.sort(key=lambda c: c["distance"])
    return candidates[:limit]


def evaluate_group(
    members: list[dict],
    leftovers: list[dict],
    topology: dict[str, Any],
    thresholds: dict[str, float],
    forced_reason: str | None = None,
) -> dict[str, Any]:
    """评估一个已成型的组。members 为该组电芯，leftovers 为所有未入组电芯。

    forced_reason: 尾料池等“非真实成包组”强制附带的风险原因 code。
    成员上的可选键 ``screening`` 为该电芯最新静置复测结论
    （``{"verdict", "batch_id", ...}``，未筛查为 None）。
    """
    s, p, group_size = topology["series"], topology["parallel"], topology["group_size"]
    caps = [c["capacity_ah"] for c in members]
    ress = [c["resistance_mohm"] for c in members]
    ocvs = [c["ocv_v"] for c in members]
    temps = [c["temperature_c"] for c in members]
    sohs = [c["soh"] for c in members]

    cap_mu, res_mu = mean(caps), mean(ress)
    cap_cv, res_cv = cv(caps), cv(ress)
    cap_min, cap_max = min(caps), max(caps)
    res_min, res_max = min(ress), max(ress)
    ocv_delta = max(ocvs) - min(ocvs)
    temp_delta = max(temps) - min(temps)

    cap_cv_excess = cap_cv - thresholds["capacity_cv_max"]

    weakest = min(
        members,
        key=lambda c: (c["capacity_ah"], -c["resistance_mohm"], c["soh"]),
    )
    deficit_pct = round((cap_mu - weakest["capacity_ah"]) / cap_mu * 100, 4) if cap_mu else 0.0

    # 按串并联拓扑计算成包可用容量/内阻与各并联串明细
    layout = _pack_layout(members, topology)
    usable_capacity_ah = layout["usable_capacity_ah"]
    pack_resistance_mohm = layout["pack_resistance_mohm"]
    pack_voltage_v = round(min(ocvs) * s, 4)  # 保守估计：按最低单体 × 串联数

    # ---- 风险评分（0-100，分段累计并截断） ----
    reasons: list[dict[str, Any]] = []
    score = 0.0

    if cap_cv_excess > 0:
        add = min(40.0, 10.0 + cap_cv_excess / max(thresholds["capacity_cv_max"], 1e-9) * 30.0)
        score += add
        reasons.append({
            "code": "CAPACITY_DISPERSION",
            "message": f"容量离散度 CV={cap_cv * 100:.2f}% 超过阈值 "
                       f"{thresholds['capacity_cv_max'] * 100:.2f}%",
            "measured": round(cap_cv, 6),
            "threshold": thresholds["capacity_cv_max"],
        })

    res_cv_excess = res_cv - thresholds["resistance_cv_max"]
    if res_cv_excess > 0:
        add = min(30.0, 8.0 + res_cv_excess / max(thresholds["resistance_cv_max"], 1e-9) * 22.0)
        score += add
        reasons.append({
            "code": "RESISTANCE_DISPERSION",
            "message": f"内阻离散度 CV={res_cv * 100:.2f}% 超过阈值 "
                       f"{thresholds['resistance_cv_max'] * 100:.2f}%",
            "measured": round(res_cv, 6),
            "threshold": thresholds["resistance_cv_max"],
        })

    if temp_delta > thresholds["temperature_delta_max"]:
        over = temp_delta - thresholds["temperature_delta_max"]
        score += min(10.0, 3.0 + over)
        reasons.append({
            "code": "TEMPERATURE_SPREAD",
            "message": f"组内测试温差 {temp_delta:.2f}°C 超过阈值 "
                       f"{thresholds['temperature_delta_max']:.2f}°C",
            "measured": round(temp_delta, 4),
            "threshold": thresholds["temperature_delta_max"],
        })

    if ocv_delta > thresholds["ocv_delta_max"]:
        over = ocv_delta - thresholds["ocv_delta_max"]
        score += min(10.0, 3.0 + over * 100)
        reasons.append({
            "code": "OCV_SPREAD",
            "message": f"开路电压差 {ocv_delta * 1000:.0f}mV 超过阈值 "
                       f"{thresholds['ocv_delta_max'] * 1000:.0f}mV",
            "measured": round(ocv_delta, 6),
            "threshold": thresholds["ocv_delta_max"],
        })

    min_soh = min(sohs)
    if min_soh < thresholds["soh_min"]:
        score += min(15.0, (thresholds["soh_min"] - min_soh) / thresholds["soh_min"] * 40.0)
        reasons.append({
            "code": "LOW_SOH",
            "message": f"组内最低 SOH={min_soh * 100:.1f}% 低于阈值 "
                       f"{thresholds['soh_min'] * 100:.0f}%",
            "measured": round(min_soh, 4),
            "threshold": thresholds["soh_min"],
        })

    if p > 1 and layout["method"] == "min_parallel_string" \
            and layout["string_imbalance_pct"] > 5.0:
        score += min(10.0, layout["string_imbalance_pct"])
        reasons.append({
            "code": "STRING_IMBALANCE",
            "message": f"并联串间容量不均衡 {layout['string_imbalance_pct']:.2f}% 偏高"
                       f"（最弱串 #{layout['weakest_string_no']} 决定整包可用容量）",
            "measured": layout["string_imbalance_pct"],
            "threshold": 5.0,
        })

    if len(members) < group_size:
        score = 100.0
        reasons.append({
            "code": "INCOMPLETE_GROUP",
            "message": f"组内仅 {len(members)} 只电芯，未满配 {group_size} 只，不能成包",
            "measured": len(members),
            "threshold": group_size,
        })

    if forced_reason == "UNASSIGNED_POOL" and len(members) >= group_size:
        # 碎片合并后数量达到一组，但因离散度越限无法满足同组约束
        score = 100.0
        reasons.append({
            "code": "UNASSIGNED_POOL",
            "message": f"{len(members)} 只电芯因离散度/温差等约束无法满足同组条件，"
                       f"全部进入尾料池待人工选配",
            "measured": len(members),
            "threshold": group_size,
        })

    # ---- 静置复测组级风险：待复测电芯默认允许成包但记入组风险 ----
    retest_members = [c for c in members
                      if (c.get("screening") or {}).get("verdict") == RETEST]
    if retest_members:
        score += min(20.0, 6.0 * len(retest_members))
        ids = "、".join(c["cell_id"] for c in retest_members)
        reasons.append({
            "code": "SCREENING_RETEST_PENDING",
            "message": f"{len(retest_members)} 只电芯静置复测结论为待复测，"
                       f"自放电尚未确认（{ids}），组级标记风险，需复测后复核",
            "measured": len(retest_members),
            "threshold": 0,
            "cell_ids": [c["cell_id"] for c in retest_members],
        })
    quarantine_members = [c for c in members
                          if (c.get("screening") or {}).get("verdict") == QUARANTINE]
    if quarantine_members:
        # 强制门禁关闭时（默认）隔离电芯不会进入成员；保留审计兜底
        score = 100.0
        ids = "、".join(c["cell_id"] for c in quarantine_members)
        reasons.append({
            "code": "SCREENING_QUARANTINED_CELL",
            "message": f"{len(quarantine_members)} 只电芯静置复测结论为隔离，"
                       f"疑似自放电/内短路异常（{ids}），禁止成包",
            "measured": len(quarantine_members),
            "threshold": 0,
            "cell_ids": [c["cell_id"] for c in quarantine_members],
        })

    score = round(min(100.0, score), 2)
    if not reasons:
        level = "LOW"
    elif score < 25:
        level = "LOW"
    elif score < 55:
        level = "MEDIUM"
    elif score < 80:
        level = "HIGH"
    else:
        level = "CRITICAL"

    candidates = _replacement_candidates(members, leftovers, thresholds, cap_mu, res_mu)

    is_complete = len(members) == group_size and forced_reason is None
    return {
        "group_size_required": group_size,
        "cell_count": len(members),
        "complete": is_complete,
        "cells": [
            {
                "cell_id": c["cell_id"],
                "capacity_ah": c["capacity_ah"],
                "resistance_mohm": c["resistance_mohm"],
                "ocv_v": c["ocv_v"],
                "cycles": c["cycles"],
                "temperature_c": c["temperature_c"],
                "soh": c["soh"],
                "screening": c.get("screening"),
            }
            for c in members
        ],
        "metrics": {
            "capacity_mean_ah": round(cap_mu, 4),
            "capacity_min_ah": round(cap_min, 4),
            "capacity_max_ah": round(cap_max, 4),
            "capacity_cv": round(cap_cv, 6),
            "resistance_mean_mohm": round(res_mu, 4),
            "resistance_min_mohm": round(res_min, 4),
            "resistance_max_mohm": round(res_max, 4),
            "resistance_cv": round(res_cv, 6),
            "ocv_delta_v": round(ocv_delta, 6),
            "temperature_delta_c": round(temp_delta, 4),
            "min_soh": round(min_soh, 4),
            "usable_capacity_ah": usable_capacity_ah,
            "usable_capacity_method": layout["method"],
            "estimated_pack_voltage_v": pack_voltage_v,
            "estimated_pack_resistance_mohm": pack_resistance_mohm,
            "weakest_string_no": layout["weakest_string_no"],
            "parallel_strings": layout["parallel_strings"],
            "string_capacity_cv": layout["string_capacity_cv"],
            "string_imbalance_pct": layout["string_imbalance_pct"],
            # 兼容旧字段名
            "estimated_soc_imbalance_pct": layout["string_imbalance_pct"],
            "screening_retest_count": len(retest_members),
            "screening_quarantined_count": len(quarantine_members),
        },
        "weakest_cell": {
            "cell_id": weakest["cell_id"],
            "capacity_ah": weakest["capacity_ah"],
            "resistance_mohm": weakest["resistance_mohm"],
            "soh": weakest["soh"],
            "cycles": weakest["cycles"],
            "capacity_deficit_vs_group_mean_pct": deficit_pct,
        },
        "risk": {
            "score": score,
            "level": level,
            "reasons": reasons,
        },
        "replacement_candidates": candidates,
    }


def _apply_screening_gate(
    cells: list[dict],
    screening_map: dict[str, Any] | None,
    enforce_screening: bool,
) -> tuple[list[dict], dict[str, Any]]:
    """应用静置复测门禁。

    - 最新结论为 ``QUARANTINE``（隔离）：enforce 时直接拒绝，不进入装箱；
      enforce=False（仅审计）时放行，但在 gating 中告警，组风险兜底隔离原因。
    - 最新结论为 ``RETEST``（待复测）：允许装箱，由组级风险暴露。
    - ``STABLE`` / 无筛查记录：正常处理（无记录在 gating.unscreened_cell_ids 留痕）。

    screening_map 的 value 为 dict：{"verdict", "batch_id", "drop_rate_v_per_day",
    "r_squared", "observation_hours", "verdict_label"}。
    返回 (放行并附加 screening 键的电芯, gating 报告)。
    """
    screening_map = screening_map or {}
    admitted: list[dict] = []
    rejected_quarantine: list[dict[str, Any]] = []
    warned_quarantine: list[dict[str, Any]] = []
    retest_ids: list[str] = []
    stable_ids: list[str] = []
    unscreened_ids: list[str] = []
    screened_ids: list[str] = []

    for c in cells:
        info = screening_map.get(c["cell_id"])
        item = dict(c)
        item["screening"] = info
        if info is None:
            unscreened_ids.append(c["cell_id"])
            admitted.append(item)
            continue
        screened_ids.append(c["cell_id"])
        verdict = info["verdict"]
        if verdict == QUARANTINE:
            if enforce_screening:
                rejected_quarantine.append({
                    "cell_id": c["cell_id"],
                    "screening": info,
                })
            else:
                warned_quarantine.append({
                    "cell_id": c["cell_id"],
                    "screening": info,
                })
                admitted.append(item)
        elif verdict == RETEST:
            retest_ids.append(c["cell_id"])
            admitted.append(item)
        else:
            stable_ids.append(c["cell_id"])
            admitted.append(item)

    gating = {
        "enforced": enforce_screening,
        "screened_cell_ids": screened_ids,
        "stable_cell_ids": stable_ids,
        "retest_cell_ids": retest_ids,
        "rejected_quarantined_cell_ids": [r["cell_id"] for r in rejected_quarantine],
        "rejected_quarantined": rejected_quarantine,
        "warned_quarantined_cell_ids": [r["cell_id"] for r in warned_quarantine],
        "warned_quarantined": warned_quarantine,
        "unscreened_cell_ids": unscreened_ids,
        "rejected_count": len(rejected_quarantine),
        "retest_count": len(retest_ids),
        "unscreened_count": len(unscreened_ids),
    }
    return admitted, gating


def build_groups(
    cells: list[dict],
    topology: dict[str, Any],
    thresholds: dict[str, float] | None = None,
    rated_capacity_ah: float | None = None,
    screening_map: dict[str, Any] | None = None,
    enforce_screening: bool = True,
) -> dict[str, Any]:
    """依据容量、内阻离散度进行贪心装箱，生成配组方案。

    流程：
    1. 应用静置复测门禁：默认拒绝最新结论为隔离的电芯，待复测电芯放行；
    2. 计算每只电芯 SOH（相对额定容量）；
    3. 按容量升序、内阻升序排列；
    4. 顺序开组，向当前组中加入不致 CV 越限且温度/OCV 可兼容的电芯；
    5. 满配封组，剩余不足一只组的电芯进入尾料组。
    """
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    group_size = topology["group_size"]

    gated_cells, gating = _apply_screening_gate(
        cells, screening_map, enforce_screening
    )

    enriched: list[dict] = []
    for c in gated_cells:
        item = dict(c)
        item["soh"] = round(c["capacity_ah"] / rated_capacity_ah, 4) if rated_capacity_ah else None
        enriched.append(item)

    # 低 SOH 电芯在装箱时仍参与，但由组风险暴露
    ordered = sorted(
        enriched, key=lambda c: (c["capacity_ah"], c["resistance_mohm"], c["cell_id"])
    )

    raw_groups: list[list[dict]] = []
    current: list[dict] = []
    for cell in ordered:
        if len(current) >= group_size:
            raw_groups.append(current)
            current = []

        trial = current + [cell]
        caps = [x["capacity_ah"] for x in trial]
        ress = [x["resistance_mohm"] for x in trial]
        temps = [x["temperature_c"] for x in trial]
        ocvs = [x["ocv_v"] for x in trial]
        compatible = (
            cv(caps) <= thresholds["capacity_cv_max"]
            and cv(ress) <= thresholds["resistance_cv_max"]
            and (max(temps) - min(temps)) <= thresholds["temperature_delta_max"]
            and (max(ocvs) - min(ocvs)) <= thresholds["ocv_delta_max"]
        )
        if not compatible and current:
            # 当前组放不下，封组并另开一组
            raw_groups.append(current)
            current = [cell]
        else:
            current = trial
    if current:
        raw_groups.append(current)

    # 满配封组，其余（含因越限提前封组产生的碎片）全部合并为一个尾料池
    complete_groups = [g for g in raw_groups if len(g) == group_size]
    tail_members = [c for g in raw_groups if len(g) < group_size for c in g]
    assigned_ids: set[str] = set()
    for g in complete_groups:
        assigned_ids.update(c["cell_id"] for c in g)
    leftovers = [c for c in enriched if c["cell_id"] not in assigned_ids]

    groups: list[dict[str, Any]] = []
    for idx, members in enumerate(complete_groups, start=1):
        result = evaluate_group(members, leftovers, topology, thresholds)
        result["group_no"] = f"G{idx:03d}"
        groups.append(result)

    if tail_members:
        # 尾料池：碎片合并，候选池即其自身，强制标记为不可成包
        forced = "UNASSIGNED_POOL" if len(tail_members) >= group_size else None
        result = evaluate_group(tail_members, [], topology, thresholds,
                                forced_reason=forced)
        result["group_no"] = "TAIL"
        groups.append(result)

    total_cells = len(enriched)
    grouped_cells = sum(g["cell_count"] for g in groups if g["complete"])
    risk_scores = [g["risk"]["score"] for g in groups if g["complete"]]
    all_complete = groups and all(g["complete"] for g in groups)

    if enriched:
        weakest_overall = min(
            enriched,
            key=lambda c: (c["capacity_ah"], -(c["resistance_mohm"]), c["soh"] or 1),
        )
        weakest_id = weakest_overall["cell_id"]
        cap_p10 = _percentile([c["capacity_ah"] for c in enriched], 0.10)
        cap_p50 = _percentile([c["capacity_ah"] for c in enriched], 0.50)
    else:
        weakest_id = None
        cap_p10 = cap_p50 = 0.0

    retest_in_groups = sum(
        g["metrics"]["screening_retest_count"] for g in groups if g["complete"]
    )
    summary = {
        "submitted_cells": len(cells),
        "total_cells": total_cells,
        "group_size": group_size,
        "complete_groups": len(complete_groups),
        "grouped_cells": grouped_cells,
        "unassigned_cells": total_cells - grouped_cells,
        "utilization_pct": round(grouped_cells / total_cells * 100, 2) if total_cells else 0.0,
        "max_risk_score": max(risk_scores, default=0.0),
        "mean_risk_score": round(mean(risk_scores), 2) if risk_scores else 0.0,
        "overall_risk_level": (
            "CRITICAL" if not all_complete
            else ("HIGH" if max(risk_scores, default=0) >= 55
                  else ("MEDIUM" if max(risk_scores, default=0) >= 25 else "LOW"))
        ),
        "usable_capacity_per_pack_ah": round(
            min((g["metrics"]["usable_capacity_ah"] for g in groups if g["complete"]),
                default=0.0), 4),
        "weakest_cell_id": weakest_id,
        "capacity_p10_ah": round(cap_p10, 4),
        "capacity_p50_ah": round(cap_p50, 4),
        "screening_rejected_count": gating["rejected_count"],
        "screening_retest_count": gating["retest_count"],
        "screening_unscreened_count": gating["unscreened_count"],
        "screening_retest_grouped_count": retest_in_groups,
    }

    return {
        "topology": topology,
        "thresholds": thresholds,
        "rated_capacity_ah": rated_capacity_ah,
        "screening_gate": gating,
        "summary": summary,
        "groups": groups,
    }
