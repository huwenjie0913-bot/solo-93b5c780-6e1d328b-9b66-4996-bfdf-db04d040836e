"""版本级负载校核（脉冲放电）。

静态指标（容量/内阻 CV、温差、OCV 差等）合格的配组装到设备后，仍可能在
脉冲放电时跌破最低包端电压或超出允许损耗。本模块基于已保存的版本快照
（开路电压、串并联拓扑、并联串内阻与可用容量），对给定放电步骤逐步校核：

- V端 = V开路 − I×R     （R 为整包内阻，由各并联串内阻之和给出）
- P损 = I²×R
- 累计 Ah = Σ I×t/3600
- 容量余量 = 整包可用容量 − 累计 Ah

尾料组/未满配组不构成完整拓扑，无法按包计算，整体跳过并说明原因。
"""
from __future__ import annotations

from typing import Any

# 越限原因 code
REASON_UNDER_VOLTAGE = "UNDER_MIN_TERMINAL_VOLTAGE"
REASON_OVER_LOSS_POWER = "OVER_LOSS_POWER"
REASON_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"

# 可承受峰值电流的限制来源 code（复用越限 code）
LIMITED_BY_VOLTAGE = REASON_UNDER_VOLTAGE
LIMITED_BY_POWER = REASON_OVER_LOSS_POWER


def _pack_snapshot(group: dict[str, Any], series: int) -> dict[str, Any]:
    """从组快照中提取整包电气参数。"""
    metrics = group["metrics"]
    min_cell_ocv = min(c["ocv_v"] for c in group["cells"])
    return {
        "ocv_v": round(min_cell_ocv * series, 4),
        # 快照内阻为各并联串内阻之和，单位 mΩ；计算时换算为 Ω
        "resistance_ohm": metrics["estimated_pack_resistance_mohm"] / 1000.0,
        "usable_capacity_ah": metrics["usable_capacity_ah"],
        "weakest_string_no": metrics.get("weakest_string_no"),
    }


def _violation(code: str, message: str, measured: float, threshold: float) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "measured": measured,
        "threshold": threshold,
    }


def _check_group(
    group: dict[str, Any],
    series: int,
    steps: list[dict[str, float]],
    min_terminal_v: float,
    max_loss_power_w: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """对单个完整成包组逐步计算，返回 (对外结果, 内部原始值)。"""
    snap = _pack_snapshot(group, series)
    ocv_v = snap["ocv_v"]
    resistance_ohm = snap["resistance_ohm"]
    usable_ah = snap["usable_capacity_ah"]

    step_results: list[dict[str, Any]] = []
    first_violation: dict[str, Any] | None = None
    cumulative_ah = 0.0

    for idx, step in enumerate(steps, start=1):
        current_a = step["current_a"]
        duration_s = step["duration_s"]

        voltage_drop_v = current_a * resistance_ohm
        terminal_v = ocv_v - voltage_drop_v
        loss_power_w = current_a ** 2 * resistance_ohm
        cumulative_ah += current_a * duration_s / 3600.0
        capacity_margin_ah = usable_ah - cumulative_ah

        reasons: list[dict[str, Any]] = []
        if terminal_v < min_terminal_v:
            reasons.append(_violation(
                REASON_UNDER_VOLTAGE,
                f"端电压 {terminal_v:.4f}V 低于最低包端电压 {min_terminal_v:.4f}V",
                round(terminal_v, 4), round(min_terminal_v, 4),
            ))
        if loss_power_w > max_loss_power_w:
            reasons.append(_violation(
                REASON_OVER_LOSS_POWER,
                f"损耗功率 {loss_power_w:.2f}W 超过上限 {max_loss_power_w:.2f}W",
                round(loss_power_w, 4), round(max_loss_power_w, 4),
            ))
        if capacity_margin_ah < 0:
            reasons.append(_violation(
                REASON_CAPACITY_EXCEEDED,
                f"累计放电 {cumulative_ah:.4f}Ah 超过可用容量 {usable_ah:.4f}Ah",
                round(cumulative_ah, 4), round(usable_ah, 4),
            ))

        violated = bool(reasons)
        record = {
            "step_no": idx,
            "current_a": round(current_a, 4),
            "duration_s": round(duration_s, 3),
            "voltage_drop_v": round(voltage_drop_v, 4),
            "terminal_voltage_v": round(terminal_v, 4),
            "loss_power_w": round(loss_power_w, 4),
            "discharged_ah_step": round(current_a * duration_s / 3600.0, 6),
            "cumulative_ah": round(cumulative_ah, 6),
            "capacity_margin_ah": round(capacity_margin_ah, 6),
            "violated": violated,
            "violations": reasons,
        }
        step_results.append(record)
        if violated and first_violation is None:
            first_violation = {
                "step_no": idx,
                "reasons": reasons,
            }

    # 可承受峰值电流：同时满足电压与损耗约束的最小电流
    # 下限高于开路电压时，即使 I=0（端电压=开路电压）也达不到下限，
    # 电压侧可承受峰值为 0，而不是负值。
    if resistance_ohm > 0:
        peak_by_voltage = max(0.0, (ocv_v - min_terminal_v) / resistance_ohm)
        peak_by_power = (max_loss_power_w / resistance_ohm) ** 0.5
    else:
        # 内阻为 0 时内阻不构成压降/损耗，约束无意义
        peak_by_voltage = float("inf")
        peak_by_power = float("inf")
    if peak_by_voltage <= peak_by_power:
        peak_current_a = peak_by_voltage
        limited_by = LIMITED_BY_VOLTAGE
    else:
        peak_current_a = peak_by_power
        limited_by = LIMITED_BY_POWER

    report = {
        "group_no": group["group_no"],
        "pack_ocv_v": ocv_v,
        "pack_resistance_mohm": round(resistance_ohm * 1000.0, 6),
        "usable_capacity_ah": usable_ah,
        "weakest_string_no": snap["weakest_string_no"],
        "steps": step_results,
        "first_violation": first_violation,
        "passed": first_violation is None,
        "total_duration_s": round(sum(s["duration_s"] for s in steps), 3),
        "total_discharged_ah": round(cumulative_ah, 6),
        "final_capacity_margin_ah": round(usable_ah - cumulative_ah, 6),
        "sustainable_peak_current_a": round(peak_current_a, 4),
        "peak_current_limited_by": limited_by,
    }
    raw = {
        "peak_current_a": peak_current_a,
        "final_capacity_margin_ah": usable_ah - cumulative_ah,
    }
    return report, raw


def _excluded_view(group: dict[str, Any], topology: dict[str, Any]) -> dict[str, Any]:
    """尾料组/未满配组的跳过说明。"""
    group_size = topology["group_size"]
    if group["group_no"] == "TAIL":
        reason = (
            f"尾料池含 {group['cell_count']} 只电芯，为离散度/温差等约束无法成包"
            f"的碎片合并，不能构成 {topology['series']}S{topology['parallel']}P "
            f"完整拓扑（{group_size} 只/包），不参与整包负载校核"
        )
    else:
        reason = (
            f"组内仅 {group['cell_count']} 只电芯，未满配 {group_size} 只，"
            f"不能构成 {topology['series']}S{topology['parallel']}P 完整拓扑，"
            f"不参与整包负载校核"
        )
    return {
        "group_no": group["group_no"],
        "cell_count": group["cell_count"],
        "complete": False,
        "reason": reason,
    }


def check_version_load(
    version_result: dict[str, Any],
    steps: list[dict[str, float]],
    min_terminal_v: float,
    max_loss_power_w: float,
) -> dict[str, Any]:
    """对已保存版本快照执行整包脉冲放电校核。

    返回逐组逐步结果、首个越限点、可承受峰值电流、最弱组，以及被跳过的尾料组说明。
    """
    topology = version_result["topology"]
    series = topology["series"]
    groups = version_result["groups"]

    complete_groups = [g for g in groups if g.get("complete")]
    excluded_groups = [_excluded_view(g, topology) for g in groups if not g.get("complete")]

    group_reports: list[dict[str, Any]] = []
    raws: list[dict[str, Any]] = []
    for group in complete_groups:
        report, raw = _check_group(
            group, series, steps, min_terminal_v, max_loss_power_w
        )
        group_reports.append(report)
        raws.append(raw)

    # 最弱组：可承受峰值电流最小；并列时容量余量更小者优先
    weakest_idx = min(
        range(len(group_reports)),
        key=lambda i: (raws[i]["peak_current_a"],
                       raws[i]["final_capacity_margin_ah"],
                       group_reports[i]["group_no"]),
    ) if group_reports else None

    for i, report in enumerate(group_reports):
        report["is_weakest_group"] = (i == weakest_idx)

    # 整组首个越限点：取所有组中最早步骤，组顺序作为并列裁决
    overall_first: dict[str, Any] | None = None
    for i, report in enumerate(group_reports):
        fv = report["first_violation"]
        if fv is None:
            continue
        if overall_first is None or fv["step_no"] < overall_first["step_no"]:
            overall_first = {
                "step_no": fv["step_no"],
                "group_no": report["group_no"],
                "reasons": fv["reasons"],
            }

    weakest_report = group_reports[weakest_idx] if weakest_idx is not None else None
    if weakest_report is not None:
        limited_by = weakest_report["peak_current_limited_by"]
        if limited_by == LIMITED_BY_VOLTAGE:
            weakest_reason = (
                f"可承受峰值电流 {weakest_report['sustainable_peak_current_a']:.2f}A "
                f"为各包最低：整包开路电压 {weakest_report['pack_ocv_v']:.3f}V、内阻 "
                f"{weakest_report['pack_resistance_mohm']:.4f}mΩ，相同电流下压降最大，"
                f"峰值电流受最低包端电压限制"
            )
        else:
            weakest_reason = (
                f"可承受峰值电流 {weakest_report['sustainable_peak_current_a']:.2f}A "
                f"为各包最低：整包内阻 {weakest_report['pack_resistance_mohm']:.4f}mΩ、"
                f"开路电压 {weakest_report['pack_ocv_v']:.3f}V，相同电流下 I²R 损耗最大，"
                f"峰值电流受最大损耗功率限制"
            )
    else:
        weakest_reason = None

    return {
        "topology": {"series": series, "parallel": topology["parallel"]},
        "limits": {
            "min_terminal_voltage_v": min_terminal_v,
            "max_loss_power_w": max_loss_power_w,
        },
        "steps_count": len(steps),
        "complete_group_count": len(complete_groups),
        "excluded_groups_count": len(excluded_groups),
        "excluded_groups": excluded_groups,
        "groups": group_reports,
        "first_violation": overall_first,
        "all_groups_passed": overall_first is None,
        "peak_current_a": weakest_report["sustainable_peak_current_a"]
        if weakest_report else None,
        "peak_current_limited_by": weakest_report["peak_current_limited_by"]
        if weakest_report else None,
        "weakest_group": (
            {
                "group_no": weakest_report["group_no"],
                "sustainable_peak_current_a":
                    weakest_report["sustainable_peak_current_a"],
                "peak_current_limited_by":
                    weakest_report["peak_current_limited_by"],
                "pack_resistance_mohm": weakest_report["pack_resistance_mohm"],
                "pack_ocv_v": weakest_report["pack_ocv_v"],
                "usable_capacity_ah": weakest_report["usable_capacity_ah"],
                "weakest_string_no": weakest_report["weakest_string_no"],
                "reason": weakest_reason,
            }
            if weakest_report else None
        ),
    }
