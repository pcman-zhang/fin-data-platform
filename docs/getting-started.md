# 快速开始：从零跑通一条数据

> 本文面向**金融系统开发者**：在一台新机器上部署平台、同步第一只标的、
> 并把它读出来。全程约 30 分钟。深入使用见 [使用指南](usage.md)，
> 全部配置项见 [配置手册](configuration.md)。

## 0. 前提

- Docker 与 Docker Compose v2；
- 一个数据源凭证（默认走 Tushare；其它源见 [数据源](data-sources.md)）；
- 端口 `8000`（管理 API / WebUI）与 `5432`（数据库）空闲。

## 1. 配置

```bash
git clone <本仓库地址> fin-data-platform && cd fin-data-platform
cp .env.example .env
```

编辑 `.env`，至少填写：

| 变量 | 说明 |
|---|---|
| `POSTGRES_PASSWORD` | 数据库密码（自定义） |
| `DATABASE_PASSWORD` | 与上一项一致（集成测试 / 迁移使用） |
| `TUSHARE_TOKEN` | Tushare token（未配置则无可用数据源） |
| `FDP_SYNC_CODES` | 首个同步标的，如 `600519.SH` |
| `FDP_SYNC_START` | 首次补数起点，如 `2026-09-01` |
| `FDP_SYNC_SOURCE` | 数据源，如 `tushare` |

`.env` 已被 `.gitignore` 忽略；凭证只经环境注入，不进入镜像与仓库。

## 2. 启动

```bash
docker compose up -d
docker compose ps            # timescaledb / redis / service / runtime 应为 healthy
docker compose logs -f runtime
```

启动顺序由编排保证：数据库就绪 → 一次性迁移与参考数据首灌（`migrate`）→
只读授权（`grant-readonly`）→ 控制面（`runtime`）与服务（`service`）。

首次启动时 `runtime` 会**启动即追平**：按 `FDP_SYNC_*` 装配同步任务，
从 `FDP_SYNC_START` 推进到最近已收盘交易日；全市场基础信息（实体注册表）
也会启动即首灌一次。

## 3. 验证

```bash
curl -s http://127.0.0.1:8000/healthz          # {"ok": true, ...}
curl -s http://127.0.0.1:8000/v1/datasets | head
```

浏览器打开 `http://127.0.0.1:8000`：总览页可看到健康状态、任务记录、数据水位、
配额与成本；交互式 API 文档在 `http://127.0.0.1:8000/api/docs`。

## 4. 观察同步

```bash
# 任务运行记录（按状态/任务过滤）
curl -s "http://127.0.0.1:8000/v1/jobs?limit=20" | python3 -m json.tool

# 数据水位（每个数据集 / 范围的增量进度）
curl -s http://127.0.0.1:8000/v1/watermarks | python3 -m json.tool
```

运行中也可以手工提交同步意图（幂等）：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/jobs/sync \
  -H 'content-type: application/json' \
  -d '{"codes": ["600519.SH"], "start": "2026-09-01", "request_id": "first-sync"}'
```

窗口终点不得晚于最近已收盘交易日；重复提交（同 `request_id` / 同窗口）返回既有运行。

## 5. 读取数据

### 5.1 REST 数据面（PIT 行查询）

```bash
curl -s "http://127.0.0.1:8000/v1/datasets/cn_equity.daily_bar/rows\
?version_mode=as_of&as_of=2026-09-30T00:00:00Z\
&entity_id=10001&fields=close,volume&start=2026-09-01&end=2026-09-30&limit=100"
```

`version_mode` 必填：`latest` / `as_of`（知识时间 ≤ `as_of` 的最新版本）/ `history`；
`entity_id` 可重复；大数据量用 `cursor` 翻页；`Accept: application/vnd.apache.arrow.stream`
可换取 Arrow IPC。Raw / Factor / 时序查询等面见 [使用指南](usage.md)。

### 5.2 SDK（Python）

```bash
pip install "fin-data-platform[sdk]"
```

```python
from datetime import date, datetime
from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()      # 直连（默认）或 REST：FDP_SDK_MODE=rest
fdp.connect()                         # 连接 + schema 兼容校验（幂等）

# Raw：规范化读取（PIT 严格，as_of 必填；复权口径缺省取字典声明）
bars = fdp.raw.read(
    "cn_equity.daily_bar",
    fields=["close", "volume"],
    entities=[10001],                 # 稳定实体标识；经管理 API 检索：
    window=(date(2026, 9, 1), date(2026, 9, 30)),   # GET /v1/entities?query=600519.SH
    as_of=datetime(2026, 10, 1, 0, 0),
)
print(bars.frame.tail())

# Factor：因子读取（可 pin 已注册算法标识）
ma20 = fdp.factors.read(
    "ma20",
    window=(date(2026, 9, 1), date(2026, 9, 30)),
    as_of=datetime(2026, 10, 1, 0, 0),
)
```

直连模式用只读 DSN（`FDP_SDK_DSN`，缺省由 `DATABASE_READ_*` 组装，未配置只读用户时回退 `DATABASE_*`）；
REST 模式用 `FDP_SDK_REST_URL`。完整接口与错误模型见 [SDK](sdk.md)。

## 6. 质量与配额

```bash
curl -s http://127.0.0.1:8000/v1/quality/summary | python3 -m json.tool   # 每日质量报告
curl -s http://127.0.0.1:8000/v1/usage | python3 -m json.tool            # 配额与成本
```

质量扫描默认手动 / 管理界面触发，可用 `FDP_QUALITY_SCHEDULE` 配置每日调度；
配额（`FDP_RATE_LIMITS` / `FDP_BUDGET_*`）用于多进程共享限流与预算告警。

## 7. 下一步

| 想做什么 | 读哪里 |
|---|---|
| 常见工作流（同步 / 派生 / 导出 / 控制面意图） | [使用指南](usage.md) |
| 全部环境变量与配置项 | [配置手册](configuration.md) |
| 新增数据源 / 数据集 / 因子 | [扩展开发](extending.md) |
| 出问题时的诊断顺序 | [排障指南](troubleshooting.md) |
| 理解设计取舍 | [系统理念](philosophy.md) · [系统架构](architecture.md) |

## 常见起步问题

- **启动为空 Runtime**：未配置 `FDP_SYNC_CODES` 时不会装配同步任务，
  可随时补配后 `docker compose restart runtime`，或经 `POST /v1/jobs/sync` 提交意图。
- **迁移 / 连库认证失败**：shell 里若曾 `export DATABASE_*`，会覆盖 `.env` 注入；
  清理环境变量后重试（优先级：shell > `.env`）。
- **同步窗口被拒绝**：终点晚于最近已收盘交易日会报错（不静默截断）——
  盘中请等收盘后重试，或用 `FDP_SYNC_SCHEDULE` 交给调度。
- **查询不到实体**：先确认全市场登记任务已跑过（`sync.reference.market_registry`）；
  写入路径会自动登记新实体，源侧读取未注册代码会显式报错，数据面按不存在的
  实体查询返回空集——以注册表为准。

---

延伸阅读：[使用指南](usage.md) · [配置手册](configuration.md) ·
[排障指南](troubleshooting.md)
