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

### 8. 版本级负载校核 `POST /plans/{plan_id}/versions/{version}/load-check`

静态指标合格的配组装到设备后，仍可能在脉冲放电时跌破电压下限。该接口对**已保存的
版本快照**（开路电压、串并联拓扑、并联串内阻、可用容量）逐步计算整包放电响应，不改动版本：

- V端 = V开路 − I×R（V开路取最低单体 OCV × 串联数，R 取各并联串内阻之和，mΩ 换算为 Ω）
- P损 = I²R；累计 Ah = Σ I×t/3600；容量余量 = 整包可用容量 − 累计 Ah

```json
{
  "steps": [
    {"current_a": 30, "duration_s": 10},
    {"current_a": 90, "duration_s": 30}
  ],
  "min_terminal_voltage_v": 24.0,
  "max_loss_power_w": 120.0
}
```

| 字段 | 含义 | 单位 | 约束 |
|---|---|---|---|
| `steps` | 放电步骤（电流+持续时间） | — | 非空数组 |
| `steps[].current_a` | 放电电流 | A | 数字，≥0（负值拒绝；0 为静置步骤） |
| `steps[].duration_s` | 持续时间 | s | 数字，>0（零时长拒绝） |
| `min_terminal_voltage_v` | 最低包端电压 | V | 必填，(0, 100000] |
| `max_loss_power_w` | 最大损耗功率 | W | 必填，(0, 1e9] |

响应按**完整电池组**逐组返回每步压降/端电压/损耗功率/本步与累计 Ah/容量余量及该步越限
原因；给出全局首个越限点（最早步骤，组顺序裁决）、各组可承受峰值电流
`min((V开路−V下限)/R, √(P上限/R))` 及其限制来源，并汇总整批可承受峰值电流与最弱组
（峰值电流最小的包，含原因）。尾料组/未满配组不参与计算，但在 `excluded_groups` 中
逐条说明跳过原因；该版本没有任何满配成包组时返回 422 `NO_COMPLETE_GROUP`。

### 9. 静置复测筛查 `POST /screenings`

退役电芯初测合格后静置数天仍可能电压回落（自放电/微短路），单次 OCV 无法识别。
该接口接收每只电芯**多条**带采样时间、OCV、温度的复测，逐只执行：

1. **温度修正**：`OCV_ref = OCV_meas + α × (T_ref − T_meas)`，把每条 OCV 修正到参考温度；
2. **松弛期剔除**：以首条采样为时间零点，`elapsed < relaxation_hours` 的样本不参与拟合；
3. **最小观察跨度**：参与拟合样本的首尾时间跨度不足则不能下结论；
4. **最小二乘拟合**：对 (时间(天), 修正后 OCV) 拟合直线，斜率即补偿后电压变化速率
   （`slope_v_per_day`，正常自放电为负；`drop_rate_v_per_day = -slope` 为下降速率），
   并给出拟合优度 `r_squared`（R²）；
5. **判定**：`STABLE` 稳定 / `RETEST` 待复测 / `QUARANTINE` 隔离，逐条给出命中阈值原因
   （`code/message/measured/threshold`）。

| 判定 | 触发条件 |
|---|---|
| `RETEST` 待复测 | 采样不足 2 条；剔除松弛期后不足 2 条；观察跨度 < 最小观察跨度；R² < 拟合质量阈值 |
| `QUARANTINE` 隔离 | 拟合可信且观察跨度足够，但下降速率 > 下降速率阈值 |
| `STABLE` 稳定 | 其余（下降速率等于阈值按不越限处理） |

请求体见 `examples/sample_screening.json`。筛查参数（全部可选，缺省取默认值）：

| 参数 | 含义 | 单位 | 默认 | 范围 |
|---|---|---|---|---|
| `relaxation_hours` | 松弛期（首条采样起算，期内样本剔除） | h | 24 | [0, 1e5] |
| `min_observation_hours` | 松弛期后最小观察跨度 | h | 72 | [0, 1e6] |
| `reference_temperature_c` | OCV 修正参考温度 T_ref | °C | 25 | [-40, 85] |
| `temperature_coefficient_v_per_c` | 温度补偿系数 α（允许负值） | V/°C | 0.001 | [-0.01, 0.01] |
| `max_voltage_drop_v_per_day` | 最大允许电压下降速率 | V/天 | 0.005 | [0, 1] |
| `min_r_squared` | 线性拟合优度下限 R² | 比例 | 0.90 | [0, 1] |

采样 `cells[].samples[]` 字段：`sampled_at`（ISO 8601，支持 `Z`，无时区按 UTC，
必须升序、不重复）、`ocv_v`（V，(0, 5]）、`temperature_c`（°C，[-40, 85]）。

响应逐只返回：`sample_count` 样本数、`relaxation_excluded_count` 松弛期剔除数、
`used_sample_count` 拟合样本数、`observation_hours` 观察跨度、`fit`（补偿后斜率/
下降速率/截距/R²/拟合用样本数/平均温度）、`verdict`/`verdict_label`、`reasons`
命中阈值原因，以及逐条 `samples`（含 `elapsed_hours`、`ocv_corrected_v`、
`used_in_fit`，原始测量原样留存）。

### 10. 筛查批次与历史查询

- `GET /screenings`：批次列表（参数留存 + 各结论计数）；
- `GET /screenings/{batch_id}`：批次详情（参数、逐只结论、拟合、命中原因、原始测量）；
- `GET /cells/{cell_id}/screenings`：单只电芯历次筛查（批次倒序，便于看稳定→隔离的变化）。

筛查数据**只追加**：新筛查生成新批次，绝不改写历史批次、原始测量，也不回写已保存的
方案版本。

### 11. 筛查门禁如何影响创建/重算配组

`POST /plans` 与 `POST /plans/{plan_id}/recompute` 在装箱前自动查询每只电芯**最新批次**
筛查结论：

- 最新为 `QUARANTINE`：**默认拒绝**，不进入任何组（含尾料池），在结果
  `screening_gate.rejected_quarantined` 中逐只留痕（含批次号与拟合指标）；
- 最新为 `RETEST`：允许成包，但所在组风险附 `SCREENING_RETEST_PENDING` 原因
  （组级风险，含电芯编号），指标 `screening_retest_count` 计数；
- `STABLE` / 从未筛查：正常放行（未筛查电芯在 `unscreened_cell_ids` 留痕，不阻断）。

可选请求字段 `enforce_screening`（布尔，默认 `true`）：置 `false` 时隔离电芯不被拒绝，
仅在 `warned_quarantined` 告警，且所在组风险兜底为 `SCREENING_QUARANTINED_CELL` /
CRITICAL，供审计场景使用。门禁结果与逐只电芯筛查快照固化进版本 `result_json`；
**之后的新筛查不改变已保存版本**，只影响下一次创建/重算。

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
- **静置复测门禁**：创建/重算时取每只电芯最新筛查批次结论。隔离电芯默认拒绝
  （不入任何组，`summary.submitted_cells` 为提交数、`total_cells` 为通过门禁数）；
  待复测电芯可成包但组级附 `SCREENING_RETEST_PENDING` 风险；结果 `screening_gate`
  记录拒绝/待复测/稳定/未筛查明细并随版本快照固化。

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
`TOPOLOGY_CONFLICT` 422、`NO_COMPLETE_GROUP` 422、`PLAN_NOT_FOUND` 404、
`VERSION_NOT_FOUND` 404、`CELL_NOT_FOUND` 404、`SCREENING_BATCH_NOT_FOUND` 404、
`SCREENING_NOT_FOUND` 404、`MISSING_QUERY` 400。

## 测试

```bash
python -m pytest tests/ -q
```

## 目录结构

```
app/
  __init__.py    # 应用工厂
  validation.py  # 字段/单位/范围/拓扑/阈值校验（含负载校核、静置复测入参）
  grouping.py    # 装箱、指标、风险评分、最弱电芯、替换候选、筛查门禁
  screening.py   # 静置复测：OCV 温度修正、松弛期剔除、速率拟合、稳定/待复测/隔离判定
  load_check.py  # 版本快照脉冲放电压降/损耗/容量余量逐步校核
  diff.py        # 两版结构化差异
  db.py          # SQLite 表结构与版本/筛查批次持久化
  routes.py      # HTTP 路由与字段级错误
examples/sample_plan.json
examples/sample_screening.json
tests/           # pytest 端到端用例
run.py
```
