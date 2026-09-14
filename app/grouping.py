"""配组核心算法：贪心装箱 + 组内均衡评估 + 风险/候选计算。"""
from __future__ import annotations

import math
from typing import Any

from .validation import DEFAULT_THRESHOLDS


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


def _position_balance(caps: list[float], resistances: list[float], s: int, p: int) -> dict:
    """模拟 p 并 × s 串布局，估算 SOC 不均。

    每串由容量 CV 最大的 p 只电芯组合而成（对 SOC 最不利的近似），
    以各串容量极差 / 均值串容量作为 SOC 不均衡估计。
    """
    group_size = s * p
    if p == 1 or group_size != len(caps):
        return {"estimated_soc_imbalance_pct": 0.0, "method": "none"}

    order = sorted(range(len(caps)), key=lambda i: caps[i])
    # 容量最分散的 p 只组成一串：最小与最大交替搭配以形成最不利串
    picked: list[int] = []
    lo, hi = 0, len(order) - 1
    take_low = True
    for _ in range(min(p, len(order))):
        picked.append(order[lo if take_low else hi])
        if take_low:
            lo += 1
        else:
            hi -= 1
        take_low = not take_low

    string_caps = [min(caps[i] for i in picked)]
    remaining = [i for i in order if i not in set(picked)]
    # 其余电芯按容量升序每 p 只成串（容量近似相等，串容量≈最小）
    for k in range(0, len(remaining), p):
        chunk = remaining[k : k + p]
        if len(chunk) == p:
            string_caps.append(min(caps[i] for i in chunk))

    if len(string_caps) < 2:
        return {"estimated_soc_imbalance_pct": 0.0, "method": "worst-case-string"}
    mu = mean(string_caps)
    imbalance = (max(string_caps) - min(string_caps)) / mu if mu else 0.0
    return {
        "estimated_soc_imbalance_pct": round(imbalance * 100, 4),
        "method": "worst-case-string",
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

    # 短板原则：并联串容量取组内最小，整包可用容量 = 最小串容量
    usable_capacity_ah = round(cap_min, 4)
    pack_voltage_v = round(min(ocvs) * s, 4)  # 保守估计：按最低单体 × 串联数
    pack_resistance_mohm = round((res_mu / p) * s, 6)

    weakest = min(
        members,
        key=lambda c: (c["capacity_ah"], -c["resistance_mohm"], c["soh"]),
    )
    deficit_pct = round((cap_mu - weakest["capacity_ah"]) / cap_mu * 100, 4) if cap_mu else 0.0

    pos = _position_balance(caps, ress, s, p)

    # ---- 风险评分（0-100，分段累计并截断） ----
    reasons: list[dict[str, Any]] = []
    score = 0.0

    cap_cv_excess = cap_cv - thresholds["capacity_cv_max"]
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

    if pos["estimated_soc_imbalance_pct"] > 5.0:
        score += min(10.0, pos["estimated_soc_imbalance_pct"])
        reasons.append({
            "code": "POSITION_IMBALANCE",
            "message": f"串位置估算 SOC 不均衡 {pos['estimated_soc_imbalance_pct']:.2f}% 偏高",
            "measured": pos["estimated_soc_imbalance_pct"],
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
            "estimated_pack_voltage_v": pack_voltage_v,
            "estimated_pack_resistance_mohm": pack_resistance_mohm,
            **pos,
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


def build_groups(
    cells: list[dict],
    topology: dict[str, Any],
    thresholds: dict[str, float] | None = None,
    rated_capacity_ah: float | None = None,
) -> dict[str, Any]:
    """依据容量、内阻离散度进行贪心装箱，生成配组方案。

    流程：
    1. 计算每只电芯 SOH（相对额定容量）；
    2. 按容量升序、内阻升序排列；
    3. 顺序开组，向当前组中加入不致 CV 越限且温度/OCV 可兼容的电芯；
    4. 满配封组，剩余不足一只组的电芯进入尾料组。
    """
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    group_size = topology["group_size"]

    enriched: list[dict] = []
    for c in cells:
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

    weakest_overall = min(
        enriched,
        key=lambda c: (c["capacity_ah"], -(c["resistance_mohm"]), c["soh"] or 1),
    )
    cap_p10 = _percentile([c["capacity_ah"] for c in enriched], 0.10)
    cap_p50 = _percentile([c["capacity_ah"] for c in enriched], 0.50)

    summary = {
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
                  else "MEDIUM" if max(risk_scores, default=0) >= 25
                  else "LOW")
        ),
        "usable_capacity_per_pack_ah": round(
            min((g["metrics"]["usable_capacity_ah"] for g in groups if g["complete"]),
                default=0.0), 4),
        "weakest_cell_id": weakest_overall["cell_id"],
        "capacity_p10_ah": round(cap_p10, 4),
        "capacity_p50_ah": round(cap_p50, 4),
    }

    return {
        "topology": topology,
        "thresholds": thresholds,
        "rated_capacity_ah": rated_capacity_ah,
        "summary": summary,
        "groups": groups,
    }
