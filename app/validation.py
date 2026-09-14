"""入参校验：字段缺失、类型/单位范围、串并联拓扑与阈值。

所有电芯物理量均按接口约定的单位接收：
- capacity_ah   容量，安时 (Ah)
- resistance_mohm 内阻，毫欧 (mΩ)
- ocv_v         开路电压，伏 (V)
- cycles        循环次数，次
- temperature_c 测试温度，摄氏度 (°C)

负载校核接口（steps 放电步骤）的单位：
- current_a     放电电流，安 (A)，不允许负值
- duration_s    持续时间，秒 (s)，必须为正
"""
from __future__ import annotations

import math
import re
from typing import Any

# 字段 -> (中文名, 物理单位, 允许最小值, 允许最大值)
NUMERIC_FIELDS: dict[str, tuple[str, str, float, float]] = {
    "capacity_ah": ("容量", "Ah", 0.0, 1000.0),
    "resistance_mohm": ("内阻", "mΩ", 0.01, 1000.0),
    "ocv_v": ("开路电压", "V", 0.0, 5.0),
    "cycles": ("循环次数", "次", 0, 100000),
    "temperature_c": ("测试温度", "°C", -40.0, 85.0),
}

REQUIRED_CELL_FIELDS = ("cell_id", *NUMERIC_FIELDS.keys())

# 阈值 -> (中文名, 单位, 下限, 上限)；None 表示不做该侧限制
THRESHOLD_FIELDS: dict[str, tuple[str, str, float | None, float | None]] = {
    "capacity_cv_max": ("容量离散度阈值(CV)", "比例", 0.0, 1.0),
    "resistance_cv_max": ("内阻离散度阈值(CV)", "比例", 0.0, 2.0),
    "temperature_delta_max": ("组内温差阈值", "°C", 0.0, 60.0),
    "ocv_delta_max": ("开路电压差阈值", "V", 0.0, 1.0),
    "soh_min": ("最低健康度", "比例", 0.0, 1.0),
}

DEFAULT_THRESHOLDS = {
    "capacity_cv_max": 0.03,
    "resistance_cv_max": 0.08,
    "temperature_delta_max": 5.0,
    "ocv_delta_max": 0.05,
    "soh_min": 0.6,
}

_TOPO_RE = re.compile(r"^\s*(\d+)\s*[sS串]\s*(\d+)\s*[pP并]\s*$")


def parse_topology(raw: Any) -> dict[str, Any]:
    """解析串并联拓扑。

    支持对象 ``{"series": n, "parallel": m}`` 与字符串 ``"8S1P"`` / ``"8串1并"``。
    返回 ``{"series": int, "parallel": int, "group_size": n*m, "raw": str}``。
    """
    if isinstance(raw, dict):
        missing = [k for k in ("series", "parallel") if k not in raw]
        if missing:
            raise ValueError(f"拓扑缺少字段: {', '.join(missing)}")
        s, p = raw["series"], raw["parallel"]
        text = f"{s}S{p}P"
    elif isinstance(raw, str):
        m = _TOPO_RE.match(raw)
        if not m:
            raise ValueError("拓扑格式应为形如 '8S1P' / '8串1并' 的字符串")
        s, p, text = int(m.group(1)), int(m.group(2)), raw.strip()
    else:
        raise ValueError("拓扑应为字符串(如 '8S1P')或对象 {series, parallel}")

    if not (isinstance(s, int) and isinstance(p, int)):
        raise ValueError("串并联数必须为正整数")
    if s <= 0 or p <= 0:
        raise ValueError("串并联数必须为正整数")
    if s * p > 10000:
        raise ValueError("单组电芯数不能超过 10000")
    return {"series": s, "parallel": p, "group_size": s * p, "raw": text}


def _is_number(v: Any) -> bool:
    # bool 是 int 的子类，必须显式排除
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_cell(raw: Any, index: int) -> tuple[dict | None, list[dict]]:
    """校验单节电芯，返回 (清洗后的电芯, 错误列表)。"""
    errors: list[dict] = []
    prefix = f"cells[{index}]"

    if not isinstance(raw, dict):
        return None, [{"field": prefix, "message": "电芯记录必须是对象"}]

    cleaned: dict[str, Any] = {}

    cell_id = raw.get("cell_id")
    if cell_id is None or (isinstance(cell_id, str) and not cell_id.strip()):
        errors.append({"field": f"{prefix}.cell_id", "message": "电芯编号缺失"})
    elif not isinstance(cell_id, str):
        errors.append({"field": f"{prefix}.cell_id", "message": "电芯编号必须是字符串"})
    else:
        cleaned["cell_id"] = cell_id.strip()

    for field, (cn, unit, lo, hi) in NUMERIC_FIELDS.items():
        if field not in raw or raw[field] is None:
            errors.append({"field": f"{prefix}.{field}", "message": f"{cn}缺失（单位：{unit}）"})
            continue
        v = raw[field]
        if not _is_number(v):
            errors.append(
                {"field": f"{prefix}.{field}", "message": f"{cn}必须是数字，单位 {unit}"}
            )
            continue
        v = float(v)
        if not (lo <= v <= hi):
            errors.append(
                {
                    "field": f"{prefix}.{field}",
                    "message": f"{cn}={v} {unit} 超出合理范围 [{lo}, {hi}] {unit}",
                }
            )
            continue
        cleaned[field] = v

    return (cleaned if not errors else None), errors


def validate_payload(payload: Any, rated_capacity_ah: float) -> tuple[dict | None, list[dict]]:
    """校验创建/重算接口的完整请求体。

    返回 ``({"cells": [...], "topology": {...}, "thresholds": {...},
    "rated_capacity_ah": float, "name": str|None}, errors)``。
    """
    errors: list[dict] = []
    cells: list[dict] = []
    if not isinstance(payload, dict):
        return None, [{"field": "$", "message": "请求体必须是 JSON 对象"}]

    cells_raw = payload.get("cells")
    if cells_raw is None:
        errors.append({"field": "cells", "message": "缺少电芯列表 cells"})
    elif not isinstance(cells_raw, list) or not cells_raw:
        errors.append({"field": "cells", "message": "cells 必须是非空数组"})
    else:
        seen: set[str] = set()
        for i, raw_cell in enumerate(cells_raw):
            cell, cell_errors = validate_cell(raw_cell, i)
            for e in cell_errors:
                errors.append(e)
            if cell:
                if cell["cell_id"] in seen:
                    errors.append(
                        {"field": f"cells[{i}].cell_id",
                         "message": f"电芯编号重复: {cell['cell_id']}"}
                    )
                seen.add(cell["cell_id"])
                cells.append(cell)

    topology: dict[str, Any] | None = None
    if "topology" not in payload or payload["topology"] is None:
        errors.append({"field": "topology", "message": "缺少串并联拓扑 topology"})
    else:
        try:
            topology = parse_topology(payload["topology"])
        except ValueError as exc:
            errors.append({"field": "topology", "message": str(exc)})

    thresholds = dict(DEFAULT_THRESHOLDS)
    raw_thresholds = payload.get("thresholds", {})
    if raw_thresholds is not None and not isinstance(raw_thresholds, dict):
        errors.append({"field": "thresholds", "message": "thresholds 必须是对象"})
    elif isinstance(raw_thresholds, dict):
        for key, (cn, unit, lo, hi) in THRESHOLD_FIELDS.items():
            if key not in raw_thresholds or raw_thresholds[key] is None:
                continue
            v = raw_thresholds[key]
            if not _is_number(v):
                errors.append({"field": f"thresholds.{key}", "message": f"{cn}必须是数字"})
                continue
            v = float(v)
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                errors.append(
                    {"field": f"thresholds.{key}",
                     "message": f"{cn}={v} {unit} 超出范围 [{lo}, {hi}]"}
                )
                continue
            thresholds[key] = v

    if not _is_number(rated_capacity_ah):
        errors.append({"field": "rated_capacity_ah",
                       "message": "额定容量必须是数字，单位 Ah"})
    elif not (0.0 < float(rated_capacity_ah) <= 10000.0):
        errors.append({"field": "rated_capacity_ah",
                       "message": "额定容量(Ah)必须在 (0, 10000] 范围内"})

    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        errors.append({"field": "name", "message": "方案名称必须是字符串"})

    if errors:
        return None, errors

    return {
        "cells": cells,
        "topology": topology,
        "thresholds": thresholds,
        "rated_capacity_ah": float(rated_capacity_ah),
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
    }, []


def validate_load_check(payload: Any) -> tuple[dict | None, list[dict]]:
    """校验版本级负载校核接口请求体。

    字段：
    - steps: 非空数组，元素含 current_a（放电电流 A，>=0）与
      duration_s（持续时间 s，>0）；
    - min_terminal_voltage_v: 最低包端电压 V，必填，(0, 100000]；
    - max_loss_power_w: 最大损耗功率 W，必填，(0, 1e9]。

    返回 ``({"steps": [...], "min_terminal_voltage_v": float,
    "max_loss_power_w": float}, errors)``。
    """
    errors: list[dict] = []
    steps: list[dict[str, float]] = []

    if not isinstance(payload, dict):
        return None, [{"field": "$", "message": "请求体必须是 JSON 对象"}]

    steps_raw = payload.get("steps")
    if steps_raw is None:
        errors.append({"field": "steps", "message": "缺少放电步骤列表 steps"})
    elif not isinstance(steps_raw, list) or not steps_raw:
        errors.append({"field": "steps", "message": "steps 必须是非空数组"})
    else:
        for i, raw_step in enumerate(steps_raw):
            prefix = f"steps[{i}]"
            if not isinstance(raw_step, dict):
                errors.append({"field": prefix, "message": "放电步骤必须是对象"})
                continue

            current = raw_step.get("current_a")
            if current is None:
                errors.append({"field": f"{prefix}.current_a",
                               "message": "放电电流缺失（单位：A）"})
            elif not _is_number(current) or not math.isfinite(float(current)):
                errors.append({"field": f"{prefix}.current_a",
                               "message": "放电电流必须是有限数字，单位 A"})
            elif float(current) < 0:
                errors.append({"field": f"{prefix}.current_a",
                               "message": f"放电电流={float(current)} A 不能为负值"})

            duration = raw_step.get("duration_s")
            if duration is None:
                errors.append({"field": f"{prefix}.duration_s",
                               "message": "持续时间缺失（单位：s）"})
            elif not _is_number(duration) or not math.isfinite(float(duration)):
                errors.append({"field": f"{prefix}.duration_s",
                               "message": "持续时间必须是有限数字，单位 s"})
            elif float(duration) <= 0:
                errors.append({"field": f"{prefix}.duration_s",
                               "message": f"持续时间={float(duration)} s 必须大于 0"})

            if (current is not None and _is_number(current)
                    and math.isfinite(float(current)) and float(current) >= 0
                    and duration is not None and _is_number(duration)
                    and math.isfinite(float(duration)) and float(duration) > 0):
                steps.append({
                    "current_a": float(current),
                    "duration_s": float(duration),
                })

    min_v = payload.get("min_terminal_voltage_v")
    if min_v is None:
        errors.append({"field": "min_terminal_voltage_v",
                       "message": "缺少最低包端电压限制（单位：V）"})
    elif not _is_number(min_v) or not math.isfinite(float(min_v)):
        errors.append({"field": "min_terminal_voltage_v",
                       "message": "最低包端电压必须是有限数字，单位 V"})
    elif not (0.0 < float(min_v) <= 100000.0):
        errors.append({"field": "min_terminal_voltage_v",
                       "message": f"最低包端电压={float(min_v)} V 超出范围 (0, 100000] V"})

    max_p = payload.get("max_loss_power_w")
    if max_p is None:
        errors.append({"field": "max_loss_power_w",
                       "message": "缺少最大损耗功率限制（单位：W）"})
    elif not _is_number(max_p) or not math.isfinite(float(max_p)):
        errors.append({"field": "max_loss_power_w",
                       "message": "最大损耗功率必须是有限数字，单位 W"})
    elif not (0.0 < float(max_p) <= 1e9):
        errors.append({"field": "max_loss_power_w",
                       "message": f"最大损耗功率={float(max_p)} W 超出范围 (0, 1e9] W"})

    if errors:
        return None, errors

    return {
        "steps": steps,
        "min_terminal_voltage_v": float(min_v),
        "max_loss_power_w": float(max_p),
    }, []
