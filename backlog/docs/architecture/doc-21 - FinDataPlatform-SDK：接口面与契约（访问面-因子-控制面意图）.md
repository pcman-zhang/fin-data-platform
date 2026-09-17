---
id: doc-21
title: FinDataPlatform SDK：接口面与契约（访问面 / 因子 / 控制面意图）
type: specification
created_date: '2026-09-17 14:32'
updated_date: '2026-09-17 14:32'
---
# FinDataPlatform SDK：接口面与契约（访问面 / 因子 / 控制面意图）

> 状态：契约定稿（2026-09-17 讨论） | 关联：doc-10 §2·§3.5、doc-12（REST 契约）、doc-11（字典）、doc-20（Runtime）、TASK-3.11（发布）
> 定位：SDK 是平台的**唯一编程入口**；REST 是其薄封装；管理台基于 REST。对外文档见 `docs/sdk.md`。

## 1. 接口面总览（三读面 + 一意图面）

| 面 | 语义 | 写权限 |
|---|---|---|
| **RawAccess** | Canonical 规范化读取：PIT（as-of / version_mode）+ 口径组合（复权、单位、跨源优先级） | 无 |
| **FactorAccess** | 因子读取：单份投影（严格 as-of 对齐）或按需计算（`materialize: none`） | 无 |
| **ReadModelAccess** | 语义版本化只读出口（doc-12 语义，消费默认入口） | 无 |
| **ControlIntent** | 回填 / 物化 / 重算**意图**（幂等任务，Runtime 执行） | 仅意图；数据写入一律平台内部执行 |

规则：

1. 读路径**永不写库**（不做 lazy 回填；`doc-10 §3.5` 读取对齐语义）；
2. 写数据必须经控制面意图（`job_runs` 审计；SDK/Client 不持写凭证，doc-10 §2「对外无写入」不变）；
3. 三个读面**同一实现**（平台 `access` 层）：SDK 直连 / REST / 平台内部调用语义一致（doc-12 §8）。

## 2. Python 接口草案（以实现为准）

```python
from datetime import date, datetime

from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()          # DSN / API Key 由环境或显式参数注入

# ① RawAccess：规范化读取（PIT + 口径组合）
bars = fdp.raw.read(
    "cn_equity.daily_bar",
    fields=["close", "volume"],
    entities=[10001],
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    adjust="qfq",                          # none | qfq | hfq | None（取字典默认）
    as_of=datetime(2025, 1, 1, 12, 0),     # 必填：PIT 严格，禁止隐式 now
)
bars.meta                                  # dataset / as_of / adjust / semantic_version / data_generation?

# ② FactorAccess：因子读取（严格对齐）
factor = fdp.factors.read(
    "ma20",                                # 因子输出名（跨数据集重名时显式 dataset=）
    entities=[10001],
    window=(date(2024, 1, 1), date(2024, 12, 31)),
    as_of=datetime(2025, 1, 1, 12, 0),
    algorithm_id=None,                      # 缺省字典 active；可 pin 历史版本复现
)
factor.meta                                 # algorithm_id / as_of / data_generation / computed_at / materialized

# ③ ReadModelAccess：消费出口（doc-12 语义）
rows = fdp.read_model.read(
    "cn_equity.daily_bar",
    version_mode="as_of", as_of=datetime(2025, 1, 1, 12, 0), fields=["close"],
)

# ④ ControlIntent：回填 / 物化（幂等，平台执行）
run = fdp.control.ensure("cn_equity.daily_bar", window=(start, end))   # 采集/回填意图
run = fdp.control.materialize("ma20", algorithm_id="ma20_v1")          # 因子物化（latest）
status = run.wait(timeout=60)              # 超时抛 Timeout（任务继续在平台侧执行）
```

## 3. 语义契约

| 维度 | 约定 |
|---|---|
| `as_of` | 必填且显式（禁止隐式 now）；语义 = 知识时间点（PIT 防前视） |
| `version_mode` | `latest / as_of / history`（doc-12 §3.2；ReadModel 必填） |
| `adjust` | 缺省取字典 `adjust.default`（如日线默认 `qfq`；指数类 `none`）；不支持组合抛 `unsupported_adjust`；组合由访问层单一实现（doc-5 Router 口径） |
| 因子对齐 | 投影知识锚 `computed_at`：`version_mode=latest` 或 `as_of >= computed_at` 可服务；`as_of < computed_at` 抛 `as_of_not_aligned`（单份投影，不做 vintage） |
| 响应元数据 | `algorithm_id / as_of / data_generation / computed_at / row_count`（与 REST 头一致，doc-12 §3.4） |
| 幂等 | `control.ensure/materialize` 幂等（`version_dimension=algorithm_id` / 窗口去重），重复提交返回既有运行 |

## 4. 异常模型

`FinDataError`（基类：`code / detail / hint / request_id`；直连与 REST 同构，REST 映射 RFC 9457，doc-12 §5）：

| code | 触发 | 提示（hint） |
|---|---|---|
| `invalid_dataset / invalid_field / invalid_as_of / invalid_version_mode` | 参数非法 | 合法取值 |
| `unsupported_adjust` | 数据集不支持该复权口径 | 支持的口径列表 |
| `not_found` | 数据集 / 实体 / 因子不存在 | 相近名称 |
| `factor_not_materialized` | 因子投影不存在（`materialize: latest`） | 触发 `control.materialize` |
| `as_of_not_aligned` | `as_of < computed_at` | 改用 `latest` / 按需因子 / 等 vintage（TASK-3.13） |
| `window_not_covered` | 请求窗口超出已物化范围 | `control.ensure/materialize` 的窗口建议 |
| `inputs_stale` | 输入水位滞后，派生不可用 | 触发输入数据集同步 |
| `job_failed / timeout` | 意图任务失败 / 等待超时 | `run_id` 与排障入口 |
| `rate_limited / upstream_unavailable` | 配额 / 源不可用 | `Retry-After` |

## 5. 与 REST 的关系

- 同一 Pydantic 模型与查询内核；REST = HTTP transport（路由见 doc-12 §2.4，访问面路由随实现定稿）；
- 直连模式与 REST 模式**仅改配置**：同一 `version_mode/as_of` 语义、同一列集（含 `algorithm_id`）、同一错误语义；
- 直连不经 Redis（可选进程内缓存）。

## 6. 版本与兼容（TASK-3.11）

- SDK 版本随平台发布（语义版本）；弃用先标注 `deprecated + sunset`，再移除；
- 兼容矩阵：`dataset semantic_version × SDK version × read_model_version`；
- 最低 Python 3.11（与平台一致）。

## 7. 明确不做（边界）

- 客户端写数据、任意 SQL、直连 DB 写；外部数据上传（需鉴权与配额，doc-15 / TASK-3.19，v2 再议）；
- vintage / 历史时点因子回溯（TASK-3.13）；
- 因子挖掘、回测、执行与交易网关（永不在平台内，README 已声明）。
