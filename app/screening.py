"""静置复测筛查：OCV 温度修正 + 松弛期剔除 + 下降速率线性拟合 + 稳定/待复测/隔离判定。

退役电芯初测合格后仍可能存在自放电异常，单次 OCV 无法识别。本模块接收每只
电芯多条带采样时间、OCV、温度的复测记录：

1. **温度修正**：把每条 OCV 修正到参考温度
   ``OCV_ref = OCV_meas + α × (T_ref − T_meas)``
   （α 为温度补偿系数，单位 V/°C，允许取负值）；
2. **松弛期剔除**：以首条采样为时间零点，剔除落入松弛期内的样本
   （elapsed < relaxation_hours），只用松弛期结束后的样本拟合；
3. **最小观察跨度**：参与拟合样本的首尾时间跨度不足时不能下结论；
4. **线性拟合**：对 (时间(天), 修正后 OCV) 做最小二乘直线拟合，
   斜率即补偿后电压变化速率（V/天，正常自放电为负），并给出 R² 拟合优度；
5. **判定**：
   - 数据不足 / 观察跨度过短 / 拟合优度不达标 → ``RETEST`` 待复测；
   - 拟合可信但下降速率超过阈值 → ``QUARANTINE`` 隔离；
   - 其余 → ``STABLE`` 稳定。
"""
from __future__ import annotations

from typing import Any

# ---- 判定结论（对外稳定 code，中文标签见 VERDICT_LABELS） ----
STABLE = "STABLE"            # 稳定
RETEST = "RETEST"            # 待复测
QUARANTINE = "QUARANTINE"    # 隔离

VERDICT_LABELS: dict[str, str] = {
    STABLE: "稳定",
    RETEST: "待复测",
    QUARANTINE: "隔离",
}

# ---- 命中阈值原因 code ----
INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"
RELAXATION_LEFT_TOO_FEW = "RELAXATION_LEFT_TOO_FEW"
OBSERVATION_SPAN_TOO_SHORT = "OBSERVATION_SPAN_TOO_SHORT"
POOR_FIT_QUALITY = "POOR_FIT_QUALITY"
SELF_DISCHARGE_RATE_EXCEEDED = "SELF_DISCHARGE_RATE_EXCEEDED"

_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0


def correct_ocv(
    ocv_v: float,
    temperature_c: float,
    reference_temperature_c: float,
    coefficient_v_per_c: float,
) -> float:
    """把实测温度下的 OCV 修正到参考温度。

    OCV_ref = OCV_meas + α × (T_ref − T_meas)
    """
    return ocv_v + coefficient_v_per_c * (reference_temperature_c - temperature_c)


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """一元最小二乘，返回 (斜率, 截距, R²)；调用方保证 len>=2 且 x 不全相同。"""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx > 0 else 0.0
    intercept = my - slope * mx

    sse = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    sst = sum((y - my) ** 2 for y in ys)
    if sst == 0.0:
        # y 完全相同：直线完全解释（残差也必然为 0），约定 R²=1
        r2 = 1.0
    else:
        r2 = max(0.0, min(1.0, 1.0 - sse / sst))
    return slope, intercept, r2


def _reason(code: str, message: str, measured: Any, threshold: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "measured": measured,
            "threshold": threshold}


def screen_cell(
    cell_id: str,
    samples: list[dict[str, Any]],
    parameters: dict[str, float],
) -> dict[str, Any]:
    """对单只电芯执行静置复测筛查。

    samples 已由校验层保证：非空、按 epoch_s 升序、时间戳不重复，
    元素含 ``sampled_at / epoch_s / ocv_v / temperature_c``。
    """
    relaxation_hours = parameters["relaxation_hours"]
    min_observation_hours = parameters["min_observation_hours"]
    reference_temperature_c = parameters["reference_temperature_c"]
    alpha = parameters["temperature_coefficient_v_per_c"]
    max_drop_rate_v_per_day = parameters["max_voltage_drop_v_per_day"]
    min_r_squared = parameters["min_r_squared"]

    total = len(samples)
    origin_epoch_s = samples[0]["epoch_s"]

    sample_view: list[dict[str, Any]] = []
    for seq, s in enumerate(samples, start=1):
        elapsed_hours = (s["epoch_s"] - origin_epoch_s) / _SECONDS_PER_HOUR
        corrected = correct_ocv(
            s["ocv_v"], s["temperature_c"], reference_temperature_c, alpha
        )
        sample_view.append({
            "seq": seq,
            "sampled_at": s["sampled_at"],
            "epoch_s": s["epoch_s"],
            "elapsed_hours": round(elapsed_hours, 6),
            "ocv_v": round(s["ocv_v"], 6),
            "temperature_c": round(s["temperature_c"], 4),
            "ocv_corrected_v": round(corrected, 6),
            # 以首条采样为零点，松弛期内样本不参与拟合
            "used_in_fit": elapsed_hours >= relaxation_hours,
        })

    kept = [r for r in sample_view if r["used_in_fit"]]
    reasons: list[dict[str, Any]] = []
    fit: dict[str, Any] | None = None
    observation_hours: float | None = None

    if total < 2:
        verdict = RETEST
        reasons.append(_reason(
            INSUFFICIENT_SAMPLES,
            f"仅 {total} 条复测采样，无法拟合电压下降速率（至少需要 2 条）",
            total, 2,
        ))
    elif len(kept) < 2:
        verdict = RETEST
        reasons.append(_reason(
            RELAXATION_LEFT_TOO_FEW,
            f"剔除 {relaxation_hours:g} 小时松弛期后仅剩 {len(kept)} 条采样，"
            f"不足以拟合电压下降速率（至少需要 2 条）",
            len(kept), 2,
        ))
    else:
        observation_hours = (
            kept[-1]["epoch_s"] - kept[0]["epoch_s"]
        ) / _SECONDS_PER_HOUR

        xs = [(r["epoch_s"] - kept[0]["epoch_s"]) / _SECONDS_PER_DAY for r in kept]
        ys = [r["ocv_corrected_v"] for r in kept]
        slope, intercept, r2 = _ols(xs, ys)
        drop_rate_v_per_day = -slope  # 电压下降为正速率
        fit = {
            "slope_v_per_day": round(slope, 8),
            "drop_rate_v_per_day": round(drop_rate_v_per_day, 8),
            "intercept_v": round(intercept, 6),
            "r_squared": round(r2, 6),
            "observation_hours": round(observation_hours, 3),
            "used_sample_count": len(kept),
            "mean_temperature_c": round(
                sum(r["temperature_c"] for r in kept) / len(kept), 4),
        }

        if observation_hours < min_observation_hours:
            verdict = RETEST
            reasons.append(_reason(
                OBSERVATION_SPAN_TOO_SHORT,
                f"松弛期后观察跨度仅 {observation_hours:.1f} 小时，小于最小观察跨度 "
                f"{min_observation_hours:g} 小时，自放电速率尚不能确认",
                round(observation_hours, 3), min_observation_hours,
            ))
        elif r2 < min_r_squared:
            verdict = RETEST
            reasons.append(_reason(
                POOR_FIT_QUALITY,
                f"电压-时间线性拟合优度 R²={r2:.4f} 低于阈值 {min_r_squared:.2f}，"
                f"复测数据波动/异常点过多，下降速率不可信",
                round(r2, 6), min_r_squared,
            ))
        elif drop_rate_v_per_day > max_drop_rate_v_per_day + 1e-9:
            verdict = QUARANTINE
            reasons.append(_reason(
                SELF_DISCHARGE_RATE_EXCEEDED,
                f"温度补偿后电压下降速率 {drop_rate_v_per_day * 1000:.2f} mV/天 "
                f"超过阈值 {max_drop_rate_v_per_day * 1000:.2f} mV/天，"
                f"疑似自放电/内短路异常，判隔离",
                round(drop_rate_v_per_day, 8), max_drop_rate_v_per_day,
            ))
        else:
            verdict = STABLE

    # 落库 / 回显不保留内部 epoch_s（仅时间戳与相对小时数对外）
    for r in sample_view:
        r.pop("epoch_s", None)

    return {
        "cell_id": cell_id,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "sample_count": total,
        "relaxation_excluded_count": total - len(kept),
        "used_sample_count": len(kept),
        "observation_hours": round(observation_hours, 3)
        if observation_hours is not None else None,
        "first_sampled_at": sample_view[0]["sampled_at"],
        "last_sampled_at": sample_view[-1]["sampled_at"],
        "fit": fit,
        "reasons": reasons,
        "samples": sample_view,
    }


def summarize(cell_results: list[dict[str, Any]]) -> dict[str, int]:
    """按判定结论统计批次数。"""
    counts = {"total": len(cell_results), STABLE: 0, RETEST: 0, QUARANTINE: 0}
    for r in cell_results:
        counts[r["verdict"]] += 1
    return {
        "total_cells": counts["total"],
        "stable_count": counts[STABLE],
        "retest_count": counts[RETEST],
        "quarantined_count": counts[QUARANTINE],
    }
