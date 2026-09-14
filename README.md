# 退役电芯配组均衡评估 API

面向退役电动车电芯梯次利用产线：录入单节电芯的容量、内阻、开路电压、循环次数与测试温度，
按容量 / 内阻离散度及串并联拓扑自动配组，评估每组可用容量、失衡风险、最弱电芯，
给出风险原因与可替换候选电芯，并支持方案版本化重算、版本差异查询与 JSON 导出。

## 技术栈

Python 3.11 · Flask 3 · SQLite3（标准库）· pytest

## 运行

```bash
pip install -r requirements.txt        # 仅依赖 flask（pytest 用于测试）
python run.py                          # 默认监听 0.0.0.0:5000
# 数据库路径可用环境变量覆盖：
CELL_GROUP_DB=/data/cells.db python run.py
```

## 单位约定（接口强制校验）

| 字段 | 含义 | 单位 | 合理范围 |
|---|---|---|---|
| `capacity_ah` | 当前可用容量 | Ah | (0, 1000] |
| `resistance_mohm` | 直流内阻 | mΩ | [0.01, 1000] |
| `ocv_v` | 开路电压 | V | (0, 5.0] |
| `cycles` | 已循环次数 | 次 | [0, 100000] |
| `temperature_c` | 测试温度 | °C | [-40, 85] |
| `rated_capacity_ah` | 标称额定容量（顶层） | Ah | (0, 10000] |

拓扑支持字符串 `"8S1P"` / `"8串1并"`，或对象 `{"series": 8, "parallel": 1}`。

阈值（均可选，缺省取默认值）：

| 阈值 | 含义 | 默认 |
|---|---|---|
| `capacity_cv_max` | 组内容量变异系数 CV 上限 | 0.03 (3%) |
| `resistance_cv_max` | 组内内阻 CV 上限 | 0.08 (8%) |
| `temperature_delta_max` | 组内最大温差 °C | 5 |
| `ocv_delta_max` | 组内最大开路电压差 V | 0.05 |
| `soh_min` | 最低健康度 SOH=容量/额定容量 | 0.60 |

## 接口

Base URL: `/api/v1`

### 1. 创建方案 `POST /plans`

请求体见 `examples/sample_plan.json`。返回 201 与版本 1 的完整评估结果；同时把电芯写入档案库。

```bash
curl -X POST http://localhost:5000/api/v1/plans \
  -H 'Content-Type: application/json' -d @examples/sample_plan.json
```

### 2. 版本重算 `POST /plans/{plan_id}/recompute`

可用新的电芯集合 / 阈值重算，生成下一版本（拓扑必须与创建时一致，否则 422）。

### 3. 查询版本结果 `GET /plans/{plan_id}/versions/{version}`

### 4. 版本列表 `GET /plans/{plan_id}/versions`

### 5. 两版差异 `GET /plans/{plan_id}/diff?from=1&to=2`

差异包含：汇总指标变化（数值带 old/new/delta）、阈值变化、组成员增删（membership）、
组指标 / 风险分 / 风险等级 / 最弱电芯变化，以及新增 / 删除的组编号（含 TAIL 尾料池）。

### 6. JSON 导出 `GET /plans/{plan_id}/versions/{version}/export`

带 `Content-Disposition: attachment; filename="plan_<id>_v<n>.json"`。

### 7. 电芯档案 `GET /cells/{cell_id}` · 健康检查 `GET /health`

## 评估模型说明

- **配组算法**：按容量升序、内阻次序贪心装箱；逐只试加入当前组，要求加入后
  容量 CV、内阻 CV、温差、OCV 差均不越限，否则封组另开。满配 `S×P` 只的组编号 `Gxxx`，
  所有未满配碎片（含因越限无法同组的异常电芯）合并为一个 `TAIL` 尾料池。
- **可用容量（按拓扑计算）**：
  - 每只并联串容量 = 串内 P 只电芯容量之和（并联叠加），内阻按并联公式 1/R=Σ1/r 计算；
  - S 串串联时各串放出容量一致，整包可用容量 = **最弱并联串容量**（例：8 只 100Ah
    按 4S2P 成组 → 每串 200Ah，整包 200Ah，而非 100Ah）；
  - 配串采用 LPT（大容量电芯优先放入当前容量和最小的串）均衡分配，最大化最弱串；
    返回 `parallel_strings` 各串明细（成员、容量、并联内阻）与 `weakest_string_no`；
  - 纯串联 P=1 时退化为最弱单体容量；尾料池/未满配无法构成完整拓扑，按最弱单体保守估计。
  - 整包电压保守取最低 OCV × 串联数，整包内阻为各串内阻之和。
- **最弱电芯**：容量最低，其次内阻最高、SOH 最低；并给出相对组容量均值的缺口百分比。
- **风险评分（0–100）**：容量/内阻 CV 越限、温差、OCV 差、最低 SOH、并联串间容量
  不均衡（`string_imbalance_pct`，P>1 时生效）分项累计；尾料 / 未满配直接 CRITICAL(100)。
  等级：LOW <25，MEDIUM <55，HIGH <80，CRITICAL ≥80。
  每条原因带 `code / message / measured / threshold`。
- **可替换候选**：从所有未入组电芯中，筛选“换入后 CV 仍不越限”的电芯，
  按到组中心（容量、内阻归一化距离）排序，最多返回 5 只及换入后的预测 CV。

## 错误响应（定位到字段）

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "入参校验失败，详见 fields",
    "fields": [
      {"field": "cells[3].resistance_mohm", "message": "内阻缺失（单位：mΩ）"},
      {"field": "topology", "message": "拓扑格式应为形如 '8S1P' / '8串1并' 的字符串"},
      {"field": "thresholds.capacity_cv_max", "message": "容量离散度阈值(CV)=5.0 比例 超出范围 [0.0, 1.0]"}
    ]
  }
}
```

错误码：`INVALID_CONTENT_TYPE` 400、`INVALID_JSON` 400、`VALIDATION_FAILED` 422、
`TOPOLOGY_CONFLICT` 422、`PLAN_NOT_FOUND` 404、`VERSION_NOT_FOUND` 404、
`CELL_NOT_FOUND` 404、`MISSING_QUERY` 400。

## 测试

```bash
python -m pytest tests/ -q
```

## 目录结构

```
app/
  __init__.py    # 应用工厂
  validation.py  # 字段/单位/范围/拓扑/阈值校验
  grouping.py    # 装箱、指标、风险评分、最弱电芯、替换候选
  diff.py        # 两版结构化差异
  db.py          # SQLite 表结构与版本持久化
  routes.py      # HTTP 路由与字段级错误
examples/sample_plan.json
tests/           # pytest 端到端用例（12 项）
run.py
```
