# SDK：编程入口与接口契约

> SDK 是平台的**唯一编程入口**：REST 与 WebUI 都是它的薄封装。
> 本文给出三个读面 + 一个意图面的契约、示例与错误模型。
> 工作流视角见 [使用指南](usage.md)；安装与部署见 [快速开始](getting-started.md)。

## 1. 接口面总览

| 面 | 语义 | 写入 |
|---|---|---|
| **访问面 · Raw** | Canonical 规范化读取：PIT（`as_of`）+ 口径组合（复权、单位、跨源优先级） | 无 |
| **访问面 · 时序查询**（panel） | 在 Raw 之上：范围序列 / 截面 / 面板 / 版本历史（vintage）/ asof join；频率、缺口策略与窗口算子在可见数据上计算 | 无 |
| **访问面 · Factor** | 因子读取：单份投影（严格 `as_of` 对齐）或按需计算 | 无 |
| **消费面 · Read Model** | 语义版本化的只读出口（消费默认入口） | 无 |
| **控制面意图** | 回填 / 物化 / 重算**意图**（幂等任务，由平台执行） | 仅意图 |

两条硬规则：

1. **读路径永不写库**：不做 lazy 回填；数据不可用时报结构化异常，而不是返回错口径的结果；
2. **写数据一律平台内执行**：客户端只提交意图，任务与结果可在平台侧审计（无写凭证外发）。

## 2. 安装与连接（双模式）

```bash
pip install "fin-data-platform[sdk]"     # 发布物与平台同版本
```

```python
from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()         # 直连（默认）或 REST（FDP_SDK_MODE=rest）
fdp.connect()                            # 连接 + schema 兼容校验（幂等；首次读取自动执行）

# 或显式配置
from fin_data_platform.sdk import SdkConfig
fdp = FinDataPlatform(SdkConfig(mode="rest", rest_url="http://127.0.0.1:8000"))
```

| 环境变量 | 说明 |
|---|---|
| `FDP_SDK_MODE` | `direct`（默认）/ `rest` |
| `FDP_SDK_DSN` | 直连只读 DSN（缺省由 `DATABASE_READ_*` / `DATABASE_*` 组装；建议只读角色） |
| `FDP_SDK_CONTROL_DSN` | 直连控制面 DSN（`meta` 写权限；仅直连模式提交意图需要） |
| `FDP_SDK_REST_URL` | REST 后端地址（`rest` 模式；默认 `http://127.0.0.1:8000`） |
| `FDP_SDK_TIMEOUT` | 请求超时（秒，默认 30） |

**双模式语义一致**：同一 `version_mode / as_of` 语义、同一结果模型
（`ResultMeta` / `FactorResultMeta`）、同一错误模型
（`FinDataError`：`code / detail / hint / request_id`）。首期差异：

- REST 模式**无 API Key**（个人平台定位）；`panel`（时序查询）仅直连可用
  （`asof_join` 为本地计算，两模式可用）；
- `control.ensure` 在 REST 模式需显式 `codes`（单代码）；`control.trigger`
  在 REST 模式不支持显式窗口（窗口 = 触发日；直连支持）；
- REST 的 raw / factor 读取受服务端行数上限（50000）约束，可用 `limit` 显式截断
  （直连同样生效并计入 `meta.warnings`）；
- 资源释放：`with FinDataPlatform.from_env() as fdp:`（或 `fdp.close()`）；
  注入的 client / engine 由调用方管理；
- 只读角色需具备 `public.alembic_version` 的 `SELECT`（连接兼容校验用，授权脚本已包含）；
- 结果对象：`.frame`（pandas）/ `.table`（Arrow，直连）/ `.meta`（Pydantic，两模式同构）；
  空结果保留列集合（`meta.columns` / 请求 `fields` / 字典 schema）；
- 契约模型可导出 JSON Schema（跨语言客户端生成）：`export_json_schema()`。

## 3. 快速示例

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
    adjust=None,                        # none | qfq | hfq | None（字典默认：行情 hfq）
    as_of=datetime(2025, 1, 1, 12, 0),  # 必填：PIT 严格，禁止隐式 now
)

# ①b Raw 对齐读取：交易日 × 标的预期行 + 状态（停牌/ST）+ 缺失不填充
aligned = fdp.raw.read(
    "cn_equity.daily_bar",
    fields=["close"],
    entities=[10001],
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    as_of=datetime(2025, 1, 1, 12, 0),
    align_calendar=True,                # 按字典 expected_dates.calendar 对齐
)   # 结果含 status=ok|suspended|missing；状态数据集可见时附 is_suspended/is_st

# ② Factor：因子读取（严格对齐；可 pin 已注册算法标识）
factor = fdp.factors.read(
    "ma20",
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    as_of=datetime(2025, 1, 1, 12, 0),
    algorithm_id=None,                  # 缺省当前版本；可 pin 已注册算法标识
)

# ③ Read Model：语义版本化的消费出口
rows = fdp.read_model.read(
    "cn_equity.daily_bar",
    version_mode="as_of",
    as_of=datetime(2025, 1, 1, 12, 0),
    fields=["close"],
)

# ④ 控制面意图：回填 / 物化（仅提交意图；执行在平台侧）
runs = fdp.control.ensure(              # 采集/回填（幂等：request_id / 同窗口去重）
    "cn_equity.daily_bar",
    window=(date(2024, 1, 1), date(2024, 12, 31)),   # 缺省：水位+1 ~ 最近已收盘
    request_id="backfill-1",
)
runs.wait(timeout=60)                   # 超时抛错且任务继续在平台侧执行
run = fdp.control.materialize("ma20")   # 因子物化（窗口 = 触发日）
run.wait(timeout=60)

# ⑤ 时序查询：范围序列 / 重采样 / 缺口策略 / 窗口算子 / 版本历史 / asof join
series = fdp.panel.get_series(          # 周频（桶锚点 = 该期最后交易日）
    "cn_equity.daily_bar",
    entities=[10001],
    fields=["close", "volume"],
    start=date(2024, 1, 1),
    end=date(2024, 12, 31),
    as_of=datetime(2025, 1, 1, 12, 0),
    freq="1w",
    fill="ffill",                       # none | ffill（仅用 as_of 可见数据）
)
panel = fdp.panel.get_panel(            # 宽表：(entity_id, field) 多级列
    "cn_equity.daily_bar", entities=[10001], fields=["close"],
    start=date(2024, 1, 1), end=date(2024, 12, 31),
    as_of=datetime(2025, 1, 1, 12, 0), shape="wide",
)
versions = fdp.panel.get_versions(      # 版本历史 / as-first-reported（vintage）
    "cn_equity.daily_bar", entities=[10001],
    start=date(2024, 1, 1), end=date(2024, 12, 31), mode="vintage",
)
# asof join（本地计算；PIT 安全默认 backward）：两侧先取 DataFrame
factors = fdp.raw.read(
    "cn_equity.adj_factor", fields=["adj_factor"], entities=[10001],
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    as_of=datetime(2025, 1, 1, 12, 0),
)
joined = fdp.panel.asof_join(
    series.frame, factors.frame, left_on="trade_date", by="entity_id",
)
```

## 4. 语义要点

| 维度 | 约定 |
|---|---|
| `as_of` | 必填显式（知识时间点，防前视）；`as_of` 之前的重述与更正按知识时间正确还原 |
| `version_mode` | `latest` / `as_of` / `history`（消费面必填，无隐式默认） |
| `adjust` | 缺省取数据集声明的口径（行情默认**后复权**：因子/研究口径，历史值稳定；前复权可显式请求；指数类无复权）；不支持的口径明确报错，不静默替换 |
| 对齐读取 | 可选 `align_calendar=True`（需显式 `window` 与 `entities`）：按字典 `coverage.expected_dates.calendar` 与按域约定的状态数据集 `{domain}.daily_status` 补齐「交易日 × 标的」预期行；非交易日无行；缺行数值为 null（转 pandas 即 NaN），**不隐式填充**；`status` = `ok` / `suspended` / `missing`（盘中停牌以当日有行情为准）；日历与状态均按同一 `as_of` 严格 PIT；仅访问面 `read` 支持 |
| 因子对齐 | 因子投影带**知识锚** `computed_at`：请求时点 `>= computed_at` 可服务；更早的时点无法由单份投影回答 → 报错（不落多版本） |
| 响应元数据 | 因子读取始终返回 `algorithm_id / algorithm_version`；另含 `as_of / data_generation / row_count` |
| 幂等 | 回填 / 物化意图可重复提交：`request_id` 命中既有运行直接返回（仅支持**单代码**提交，多代码请分别提交）；否则按同窗口（`job_key`）返回既有运行（`created=False`） |
| 控制面意图 | 只提交意图（写 `meta` 队列），数据写入一律平台内执行；窗口缺省 = 水位+1 ~ **最近已收盘交易日**（落库日历 + 16:30 CST 截止），显式终点晚于最近已收盘报 `invalid_window`（不静默截断）；`wait(timeout)` 超时抛错且任务继续执行；**全局任务**（如全市场登记）用 `trigger(job_id)`（窗口 = 触发日），按代码任务用 `ensure`、因子用 `materialize` |
| 输入滞后 | 按需计算（`materialize: none`）前校验**数据输入**的可见覆盖（`knowledge_time <= as_of` 的最晚事件时间）：请求窗口终点超出覆盖报 `inputs_stale`（提示触发输入同步 / `ensure`） |
| 时序查询 | 范围序列 / 截面 / 面板 / 版本历史 / asof join：`freq ∈ {1d,1w,1mo,1q,1y}`（桶锚点 = 该期**最后一个交易日**；聚合按字段语义，可 `agg` 覆盖）；`fill ∈ {none,ffill}`（仅用可见数据）；窗口算子（rolling / change）在重采样与填充之后按实体流式计算；vintage = 每个事件日的**首个可见版本**；asof join 默认 `backward`（PIT 安全；`forward`/`nearest` 会引用未来数据）；**不使用非 PIT 的数据库连续聚合** |

## 5. 异常模型

所有错误都带 `code / detail / hint / request_id`；`hint` 给出可执行的下一步
（触发哪类任务、支持的口径列表等）。

| `code` | 触发 |
|---|---|
| `invalid_dataset / invalid_field / invalid_as_of / invalid_version_mode` | 参数非法 |
| `unsupported_adjust` | 数据集不支持该复权口径，或字段不可复权 |
| `unsupported_pit_class` | 该数据集的 PIT 类别暂不支持规范化读取 |
| `unsupported_alignment` | 数据集形态不支持日历对齐（业务键非「实体 × 事件时间」；声明的日历不在字典；字段名与保留列冲突） |
| `job_not_registered` | 数据集 / 代码 / 因子未随 Runtime 装配（提示检查代码清单或装配） |
| `invalid_window` | 窗口非法：起止颠倒，或终点晚于最近已收盘交易日 |
| `invalid_request` | 请求语义不支持（如 `request_id` 与多代码提交组合） |
| `unsupported_frequency / invalid_fill / invalid_argument` | 时序查询参数非法 |
| `invalid_alignment_scope / alignment_calendar_unavailable` | 对齐读取缺少显式 `window` / `entities`，或日历在 `as_of` 不可见 |
| `factor_not_materialized` | 因子尚未物化（提示：触发物化） |
| `as_of_not_aligned` | 请求时点早于因子投影的知识锚 |
| `window_not_covered` | 请求窗口超出投影表级覆盖范围 |
| `inputs_stale / upstream_stale` | 输入滞后 / 上游因子升级未重算（提示对应重算） |
| `timeout` | 意图等待超时（附任务标识；任务继续在平台侧执行） |
| `schema_incompatible` | SDK 版本与 schema 修订区间不兼容（提示升级方向） |
| `control_unavailable` | 未配置控制面连接（直连模式提交意图需要 `FDP_SDK_CONTROL_DSN`） |
| `upstream_unavailable` | 上游数据源不可用 |

## 6. 版本与兼容（SDK ↔ schema）

- **兼容矩阵**：`fin_data_platform.sdk.compat.COMPATIBILITY_MATRIX`（SDK 版本 →
  支持的 schema 修订区间，单一事实源）；连接时校验：直连读 `alembic_version`，
  REST 用 `/v1/health` 的 `schema_revision` 自检；不兼容抛 `schema_incompatible`
  （提示升级方向，不静默降级）；
- **弃用流程**：字段 / 语义变更先标注 `deprecated + sunset`（数据字典 / OpenAPI），
  语义变更 `semantic_version+1`，新旧并存过渡；读模型 `_vN` 对调用方透明（URL 不变）；
  SDK 随平台同版本发布，版本说明随 release 提供；
- **最低 Python 3.11**（与平台一致）。

## 7. 边界

- 不提供客户端写数据、任意 SQL、直连数据库写入；
- 不提供历史时点（vintage）**因子**回溯——因子为单份投影；
  复现依赖 pin 已注册算法标识与按需重算；
- 对齐读取不做隐式填充（ffill / interpolate 由消费侧显式选择）；缺失的补救方式是
  控制面意图（回填 / 同步任务），不是读路径；
- 因子输入当前按「存在行」读取：停牌等无 bar 日不参与计算（严格不输出）；
- 因子挖掘、回测与交易执行不属于本平台范围。

---

延伸阅读：[使用指南](usage.md) · [系统架构](architecture.md) ·
[配置手册](configuration.md)
