# FinDataPlatform SDK

> 状态：契约定稿，实现建设中。
> SDK 是平台的**唯一编程入口**：REST 与 WebUI 都是它的薄封装。

## 1. 接口面总览（三个读面 + 一个意图面）

| 面 | 语义 | 写入 |
|---|---|---|
| **访问面 · Raw** | Canonical 规范化读取：PIT（`as_of`）+ 口径组合（复权、单位、跨源优先级） | 无 |
| **访问面 · Factor** | 因子读取：单份投影（严格 `as_of` 对齐）或按需计算 | 无 |
| **消费面 · Read Model** | 语义版本化的只读出口（消费默认入口） | 无 |
| **控制面意图** | 回填 / 物化 / 重算**意图**（幂等任务，由平台执行） | 仅意图 |

两条硬规则：

1. **读路径永不写库**：不做 lazy 回填；数据不可用时报结构化异常，而不是返回错口径的结果；
2. **写数据一律平台内执行**：客户端只提交意图，任务与结果可在平台侧审计（无写凭证外发）。

## 2. 快速示例

```python
from datetime import date, datetime

from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()

# ① Raw：规范化读取（自动叠加复权口径；缺省取字典声明）
bars = fdp.raw.read(
    "cn_equity.daily_bar",
    fields=["close", "volume"],
    entities=[10001],
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    adjust="qfq",                       # none | qfq | hfq | None（字典默认）
    as_of=datetime(2025, 1, 1, 12, 0),  # 必填：PIT 严格，禁止隐式 now
)

# ② Factor：因子读取（严格对齐；可 pin 算法版本复现）
factor = fdp.factors.read(
    "ma20",
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    as_of=datetime(2025, 1, 1, 12, 0),
    algorithm_id=None,                  # 缺省当前版本；pin 旧版本可复现历史结果
)

# ③ Read Model：语义版本化的消费出口
rows = fdp.read_model.read(
    "cn_equity.daily_bar",
    version_mode="as_of",
    as_of=datetime(2025, 1, 1, 12, 0),
    fields=["close"],
)

# ④ 控制面意图：回填 / 物化（幂等；执行在平台侧）
run = fdp.control.materialize("ma20")
status = run.wait(timeout=60)
```

## 3. 语义要点

| 维度 | 约定 |
|---|---|
| `as_of` | 必填显式（知识时间点，防前视）；`as_of` 之前的重述与更正按知识时间正确还原 |
| `version_mode` | `latest` / `as_of` / `history`（消费面必填，无隐式默认） |
| `adjust` | 缺省取数据集声明的口径（行情默认前复权，指数类无复权）；不支持的口径明确报错，不静默替换 |
| 因子对齐 | 因子投影带**知识锚** `computed_at`：请求时点 `>= computed_at` 可服务；更早的时点无法由单份投影回答 → 报错（不落多版本；历史时点回溯见路线图） |
| 响应元数据 | 因子读取始终返回 `algorithm_id`；另含 `as_of / data_generation / row_count` |
| 幂等 | 回填 / 物化意图可重复提交：同窗口 / 同算法版本的重复请求命中既有任务 |

## 4. 异常模型

所有错误都带 `code / detail / hint / request_id`；`hint` 给出可执行的下一步（触发哪类任务、支持的口径列表等）。

| `code` | 触发 |
|---|---|
| `invalid_dataset / invalid_field / invalid_as_of / invalid_version_mode` | 参数非法 |
| `unsupported_adjust` | 数据集不支持该复权口径 |
| `factor_not_materialized` | 因子尚未物化（提示：触发物化） |
| `as_of_not_aligned` | 请求时点早于因子投影的知识锚 |
| `window_not_covered` | 请求窗口超出已物化范围（含缺口） |
| `inputs_stale` | 输入数据滞后，派生不可用（提示：触发输入同步） |
| `job_failed / timeout` | 意图任务失败 / 等待超时（附任务标识） |
| `rate_limited / upstream_unavailable` | 配额限制 / 上游数据源不可用 |

## 5. 边界

- 不提供客户端写数据、任意 SQL、直连数据库写入；
- 不提供历史时点（vintage）因子回溯——因子为单份投影 + 算法版本复现；
- 因子挖掘、回测与交易执行不属于本平台范围。
