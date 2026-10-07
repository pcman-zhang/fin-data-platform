# 使用指南：常见工作流

> 本文按"任务"组织：接入数据、查询、派生、质量、导出、配额观测。
> 每节给出可直接执行的 REST / SDK 片段。接口语义细节见 [SDK](sdk.md)；
> 部署与配置见 [快速开始](getting-started.md) 与 [配置手册](configuration.md)。

## 0. 三条使用路径

| 路径 | 入口 | 用途 |
|---|---|---|
| **治理**（控制面） | 管理 API / WebUI（`http://127.0.0.1:8000`） | 看任务、水位、质量、实体、配额；触发同步 / 质量 / 全局任务 |
| **消费**（只读） | SDK / REST 数据面 / 批量导出 | 读 Raw / Factor / Read Model / 时序；导出研究数据 |
| **意图**（写数据） | SDK `control.*` / `POST /v1/jobs/*` | 回填 / 物化 / 触发——只提交意图，执行在平台侧 |

两条硬规则：**读路径永不写库**（缺数据报结构化异常，不做 lazy 回填）；
**写数据一律平台内执行**（客户端不持有写权限）。

## 1. 接入与同步

### 1.1 首灌与增量

在 `.env` 配置（或运行中提交意图）：

```bash
# 运行中提交同步意图（幂等：request_id / 同窗口去重）
curl -s -X POST http://127.0.0.1:8000/v1/jobs/sync \
  -H 'content-type: application/json' \
  -d '{"codes": ["600519.SH"], "start": "2026-06-01", "end": "2026-09-30"}'
```

- 窗口缺省 = 水位 + 1 ~ 最近已收盘交易日；终点晚于最近已收盘会被拒绝
（管理端点返回 422，经 SDK 表现为 `invalid_window`）；
- 配置 `FDP_SYNC_SCHEDULE`（cron / `interval:<秒>`）后由 Runtime 周期推进，
  启动即追平历史缺口；
- 每个标的按数据集装配独立任务（日线 / 复权因子 / 每日状态），依赖与幂等由平台管理。

### 1.2 全市场基础信息（实体注册表）

```bash
curl -s -X POST http://127.0.0.1:8000/v1/jobs/trigger \
  -H 'content-type: application/json' \
  -d '{"job_id": "sync.reference.market_registry"}'
```

启动即首灌一次；可配 `FDP_REGISTRY_SCHEDULE` 周期刷新（幂等，窗口 = 触发日）。

### 1.3 观察与对账

```bash
curl -s "http://127.0.0.1:8000/v1/jobs?job_id=sync.cn_equity.daily_bar.600519.SH&limit=10"
curl -s http://127.0.0.1:8000/v1/watermarks
```

任务状态机：`queued → running → succeeded / failed → retrying → dead`；
`dead` 任务重放：按代码同步用 `POST /v1/jobs/sync`（或 SDK `control.ensure`），
全局任务（全市场登记 / 质量）用 `POST /v1/jobs/trigger`；
导出失败经 `POST /v1/exports` 重新提交（导出非幂等，每次新建请求）。

## 2. 查询数据

### 2.1 选哪一层？

| 需求 | 用哪面 | 说明 |
|---|---|---|
| 研究 / 回测取数（防前视） | **Raw 读取**（`as_of`） | 规范化 + 复权等口径组合，PIT 严格 |
| 交易日 × 标的完整序列 | **Raw 对齐读取**（`align_calendar=True`） | 停牌 / 缺失标注，不填充 |
| 因子值 | **Factor 读取** | 单份投影严格对齐；可 pin 已注册算法标识 |
| 稳定出口 / 跨版本兼容 | **Read Model** | 语义版本化，消费默认入口 |
| 重采样 / 缺口 / 窗口 / vintage / asof join | **时序查询**（panel，SDK 直连） | 在可见数据上计算 |

### 2.2 REST（数据面）

```bash
# PIT 行查询：version_mode 必填
curl -s "http://127.0.0.1:8000/v1/datasets/cn_equity.daily_bar/rows\
?version_mode=as_of&as_of=2026-09-30T00:00:00Z\
&entity_id=10001&fields=close,volume&start=2026-09-01&end=2026-09-30"

# Raw 读取（复权口径组合；缺省取字典声明）
curl -s "http://127.0.0.1:8000/v1/raw/cn_equity.daily_bar/rows\
?as_of=2026-09-30T00:00:00Z&entity_id=10001&adjust=hfq"

# Factor 读取（未物化报 404，不 lazy 回填）
curl -s "http://127.0.0.1:8000/v1/factors/ma20/rows\
?as_of=2026-09-30T00:00:00Z&entity_id=10001"
```

分页用 `cursor`；`Accept: application/vnd.apache.arrow.stream` 或 `format=arrow`
返回 Arrow IPC；`ETag` / `If-None-Match` 支持 304 缓存；行数上限 50000。

### 2.3 SDK

```python
from datetime import date, datetime
from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()
as_of = datetime(2026, 10, 1)

# 对齐读取：交易日 × 标的 + 状态（ok / suspended / missing）
aligned = fdp.raw.read(
    "cn_equity.daily_bar", fields=["close"], entities=[10001],
    window=(date(2026, 9, 1), date(2026, 9, 30)), as_of=as_of,
    align_calendar=True,
)

# 时序查询（直连）：周频重采样 + 前向填充（仅用可见数据）
series = fdp.panel.get_series(
    "cn_equity.daily_bar", entities=[10001], fields=["close", "volume"],
    start=date(2026, 6, 1), end=date(2026, 9, 30), as_of=as_of,
    freq="1w", fill="ffill",
)

# vintage：每个事件日的首个可见版本（as-first-reported）
versions = fdp.panel.get_versions(
    "cn_equity.daily_bar", entities=[10001],
    start=date(2026, 6, 1), end=date(2026, 9, 30), mode="vintage",
)
```

### 2.4 实体与 PIT 宇宙

```bash
curl -s "http://127.0.0.1:8000/v1/entities?query=600519.SH"        # 代码 → 稳定实体
curl -s "http://127.0.0.1:8000/v1/entities/universe?as_of=2026-09-30"  # 历史时点在市标的
```

`knowledge_as_of` 可选：严格按知识时间还原当时可见的标的集合。

## 3. 派生（因子）

### 3.1 读取

见 §2.3；因子投影带知识锚（`computed_at`），请求时点早于锚点报 `as_of_not_aligned`；
响应始终含 `algorithm_id / algorithm_version`。

### 3.2 物化与重算（控制面意图）

```python
runs = fdp.control.materialize("ma20")   # 触发物化（窗口 = 触发日）
runs.wait(timeout=300)
```

```bash
# REST 等价入口：物化意图（幂等；因子未注册 404 / 任务未装配 409）
curl -s -X POST http://127.0.0.1:8000/v1/jobs/materialize \
  -H 'content-type: application/json' \
  -d '{"factor": "ma20", "dataset": "cn_equity.daily_bar"}'
```

`materialize: none` 的因子按需计算（输入严格 as-of）；`latest` 因子在首次物化前
无投影可读——物化是显式意图，不会在读路径上自动发生。

### 3.3 算法升级与复现

- 升级 = 新增 `algorithm_id`（带版本），旧实现永久保留；
- 复现历史：读取时 pin 已注册的算法标识（不同版本以不同标识注册）；
  投影为单份，不支持历史多版本回溯；
- 升级事件（生效日 / 原因）写入重述台账，可在 WebUI 算法页查看；
- 上游升级而下游未重算时，读取报 `upstream_stale`，提示先重算上游。

## 4. 质量

```bash
# 触发扫描（run 窗口 = 触发日；扫描终点不晚于最近已收盘；也可配置每日调度）
curl -s -X POST http://127.0.0.1:8000/v1/jobs/trigger \
  -H 'content-type: application/json' \
  -d '{"job_id": "quality.scan"}'

# 每日报告与明细
curl -s http://127.0.0.1:8000/v1/quality/summary
curl -s "http://127.0.0.1:8000/v1/quality/results?dataset=cn_equity.daily_bar&status=failed"
```

检查族（`family`）：`rule`（字典值规则：`unique` / `not_null` / `range` / `enum` /
`expression` / `jump`）、`completeness`（完整性覆盖率与断点）、`freshness`（时效性）、
`reconcile`（对账目标存在性）、`cross_source`（跨源对账）。结果 append-only，可追溯"哪条规则、哪个窗口、哪些行"。

## 5. 批量导出（研究通道）

```bash
# 提交导出（异步；version_mode 缺省 latest）
curl -s -X POST http://127.0.0.1:8000/v1/exports \
  -H 'content-type: application/json' \
  -d '{"dataset": "cn_equity.daily_bar", "fields": ["close", "volume"],
       "start": "2026-06-01", "end": "2026-09-30", "format": "parquet"}'
# → {"export_id": "...", "status": "pending"}

EXPORT_ID=你的导出ID
curl -s "http://127.0.0.1:8000/v1/exports/$EXPORT_ID"              # 状态 / 行数 / 大小
curl -sOJ "http://127.0.0.1:8000/v1/exports/$EXPORT_ID/download"   # 产物（Parquet / Arrow）
```

- 产物按实体批 × 时间块分块写出（内存有界，不逐标的拉取），失败原子替换；
- 单块被查询上限截断即报错（不静默截断）；
- 导出**非幂等**：重复提交会新建请求（`request_id` 仅登记）；
- 产物目录 `FDP_EXPORT_DIR`（compose 默认共享卷 `/data/exports`）。

## 6. 配额与成本

```bash
curl -s http://127.0.0.1:8000/v1/usage | python3 -m json.tool
```

按源返回：今日调用 / 成本、预算与剩余、派生告警（`alerts`，多进程一致）、
触发计数（`fired`，审计）、限流配置与共享缓存可用性（`shared=false` 表示
fail-open 降级）。配置见 [配置手册 §6.7](configuration.md)。

## 7. 任务速查表

| 任务 | 触发方式 | 幂等键 |
|---|---|---|
| 按代码同步（日线 / 因子 / 状态） | `POST /v1/jobs/sync`；`FDP_SYNC_SCHEDULE` | 代码 × 窗口 |
| 全市场基础信息 | `sync.reference.market_registry` 触发；启动即首灌 | 窗口（触发日） |
| 质量扫描 | `quality.scan` 触发；`FDP_QUALITY_SCHEDULE` | 调度窗口 = 最近已收盘；手工触发 run 窗口 = 触发日（扫描终点不晚于最近已收盘） |
| 因子物化 | `control.materialize` / 派生任务触发；`FDP_DERIVE_SCHEDULE` | 算法标识 × 窗口 |
| 批量导出 | `POST /v1/exports` | 非幂等（每次新建） |

---

延伸阅读：[SDK](sdk.md) · [配置手册](configuration.md) ·
[数据源](data-sources.md) · [排障指南](troubleshooting.md)
