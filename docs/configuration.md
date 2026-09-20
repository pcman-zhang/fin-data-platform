# 配置手册

> 本文说明各组件的配置方式与全部配置项。
> 组件职责见 [核心组件](components.md)；故障处理见 [排障指南](troubleshooting.md)。

## 0. 总原则

- **凭证一律显式注入**：通过配置对象参数或环境变量，由调用方提供；
  库不会隐式读取用户目录、全局配置文件或任何外部状态；
- **凭证不落仓库、不进日志**：配置对象的 `repr` 已脱敏；
- **配置错误快速失败**：Runtime 配置非法时以非零码退出并给出明确原因。

## 1. 交付形态与安装

平台以**容器方式交付**：单镜像多入口（控制面 / 调度 / 执行与消费服务）与数据库
由 Docker Compose 编排，凭证与配置经环境变量注入（编排建设中，见
[系统架构](architecture.md) §8）。

开发环境（源码方式，用于贡献代码与运行测试）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,platform,tushare,akshare,ifind,wind,fuyao,baostock]"
```

## 2. 接入层（FinDataHub）

### 2.1 凭证

```python
from fin_data_hub import FinDataHub, HubConfig
from fin_data_hub.config import TushareConfig, WindConfig, IfindConfig, FuyaoConfig

config = HubConfig(
    tushare=TushareConfig(token="..."),
    wind=WindConfig(api_key="..."),
    ifind=IfindConfig(authorization="..."),
    fuyao=FuyaoConfig(api_key="..."),
    # AkShare 与 BaoStock 无需凭证
)
hub = FinDataHub.from_config(config)   # 缺凭证 / 缺依赖的源自动跳过
```

| 配置对象 | 参数 | 说明 |
|---|---|---|
| `TushareConfig` | `token` | Tushare 接口凭证 |
| `WindConfig` | `api_key` | Wind MCP 凭证 |
| `IfindConfig` | `authorization` | iFinD MCP 凭证 |
| `FuyaoConfig` | `api_key` / `base_url` / `max_attempts` | 可覆盖服务地址与重试次数 |
| `BaostockConfig` | `max_attempts` | 会话自动重连 |
| `AkShareConfig` | — | 本地库，无需凭证 |

### 2.2 缓存（`CacheConfig`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `max_bytes` | 512 MiB | 字节预算（主约束） |
| `max_entries` | 4096 | 条数兜底 |
| `max_entry_bytes` | 64 MiB | 单条过大跳过缓存 |
| `ttl` | 6 小时 | 付费源默认缓存时长 |
| `copy_on_return` | `False` | 返回副本以隔离调用方修改 |

调用级覆盖：`hub.get_bars(..., force=True)` 跳过缓存并刷新。

### 2.3 限流（`rate_limits`）

每源独立令牌桶，在真实调用前获取令牌，等待超时抛 `RateLimitTimeout`。

| 源 | 默认 QPS |
|---|---|
| Tushare | 2 |
| AkShare | 1 |
| Wind | 1 |
| iFinD | 2 |

```python
from fin_data_hub.ratelimit import RateLimitConfig

config = HubConfig(rate_limits={"ifind": RateLimitConfig(rate=2.0, burst=2.0, timeout=30.0)})
```

注意：限流与预算是**进程内**的；多进程部署会叠加实际请求量。

### 2.4 预算与计量（`BudgetConfig`）

| 参数 | 说明 |
|---|---|
| `calls_per_day` | 按源的日调用次数上限 |
| `cost_per_day` | 按源的日成本上限 |
| `cost_table` | `"source.endpoint"` → 单次成本；支持按源兜底 |
| `warn_ratio` | 达到预算比例时告警（默认 0.8） |
| `on_alert` | 告警回调 |
| `on_record` | 每条记录回调（跨进程汇总的集成点） |

`hub.stats()` 查看当前进程的调用、成本与告警统计。

### 2.5 跨源路由（`RoutingConfig`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `factor_source` | `tushare` | 复权合成使用的因子源 |
| `trusted_native_adjust` | `{tushare, akshare}` | 允许使用原生复权的源 |
| `fallbacks` | 空 | 主源失败时的回退顺序 |
| `field_fill` | `True` | 允许从补充源补全缺失字段 |

`source` 始终是主源；补全、合成与回退不改变主源语义，并在结果中标注实际执行源。
各源复权可信度与已知问题见 [数据源](data-sources.md)。

## 3. 平台存储与数据库

### 3.1 连接配置

平台由环境变量构建连接（`StorageConfig.from_env`）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `DATABASE_HOST` | ✅ | 数据库主机 |
| `DATABASE_PORT` | | 默认 5432 |
| `DATABASE_USER` | ✅ | 用户名 |
| `DATABASE_PASSWORD` | ✅ | 密码 |
| `DATABASE_NAME` | | 默认 `fin_data_platform` |
| `DATABASE_CONNECT_TIMEOUT` | | 连接超时秒数（默认 5；网络不可达时快速失败） |
| `DATABASE_READ_USER` / `DATABASE_READ_PASSWORD` | | 只读登录用户（读写 DSN 分离；缺省沿用写端） |
| `DATABASE_READ_HOST` / `DATABASE_READ_PORT` / `DATABASE_READ_NAME` | | 只读连接覆盖（可缺省） |
| `FDP_DATABASE_HOST` | | 覆盖主机（地址变动的场景） |

`StorageConfig` 同时支持读写 DSN 分离（`write_dsn` / `read_dsn`）与
`timescale` 开关（是否启用分区 / 压缩语句）。

### 3.2 本地开发数据库

开发数据库（仅 PostgreSQL + TimescaleDB）与全栈编排相互隔离，端口默认退避，
可与全栈同时运行：

```bash
cp .env.example .env                            # 填写密码（.env 不入库）
docker compose -f docker-compose.dev.yml up -d  # 开发库：默认端口 15432（DEV_POSTGRES_PORT）
export $(grep -v '^#' .env | xargs)
```

| 环境 | 项目名 | 默认端口 | 说明 |
|---|---|---|---|
| 开发库 | `fin-data-platform-dev` | 15432 | 仅数据库，供本地开发与集成测试 |
| 全栈 | `fin-data-platform` | 5432 | 数据库 + Redis + 迁移 + Runtime |

集成测试与迁移的 `DATABASE_PORT` 需与所用环境一致（默认指向 5432 全栈库；
使用开发库时改为 15432）。

> 注意：开发库项目更名为 `fin-data-platform-dev` 后启用**新的数据卷**；
> 原 `fin-data-platform_*` 卷归全栈环境使用（如需回迁，可手动复制卷内容）。

### 3.3 版本化迁移

```bash
.venv/bin/python -c "from fin_data_platform.storage.migrations import upgrade; upgrade()"
.venv/bin/python -c "from fin_data_platform.storage.migrations import downgrade; downgrade()"
```

| 变量 | 说明 |
|---|---|
| `FDP_ALEMBIC_INI` | 指定 `alembic.ini`（部署容器内打包路径不同时使用） |
| `FDP_ALEMBIC_SCRIPT_LOCATION` | 指定迁移脚本目录 |
| `FDP_TEST_DATABASE=1` | **仅测试**：允许破坏性迁移集成测试（会清空项目表） |

基线由数据字典生成；字典变更必须新增迁移修订。

### 3.4 权限与读写 DSN 分离

**当前实现：只读 / 可写两分**

| 角色 | 类型 | 权限 | 用途 |
|---|---|---|---|
| `<writer>`（`DATABASE_USER`） | 登录用户 | 各 schema 读写 | 采集 / 派生 / 迁移 |
| `fdp_ro` | NOLOGIN 权限角色 | `mart` + `ref` + 各数据域 canonical 的 `USAGE/SELECT`、`mart` 函数 `EXECUTE`；**不含** `raw` / `meta` | 只读授权载体 |

只读登录用户由调用方/运维创建并继承权限角色（凭证不经过代码与仓库）：

```sql
CREATE ROLE app_ro LOGIN PASSWORD '...';
GRANT fdp_ro TO app_ro;
```

- **DSN 分离**：配置 `DATABASE_READ_USER` / `DATABASE_READ_PASSWORD` 后，
  `create_read_engine` 使用只读连接——写入会被**数据库**拒绝（不是代码约定）；
- **授权执行**：`python -m fin_data_platform.storage.grants`（幂等；容器中由
  一次性服务 `grant-readonly` 在迁移后自动执行）；新表/视图经
  `DEFAULT PRIVILEGES` 自动覆盖。

**目标架构（文档化，后续演进）**：按域拆分只读角色 `fdp_ro_<domain>`
（基础角色 + 单域 canonical），实现逐域最小授权；个人平台现阶段收敛为
只读 / 可写两分，避免角色矩阵过度复杂化。

## 4. 容器化部署（Docker Compose）

平台以单镜像多入口交付；数据库、一次性迁移与 Runtime 由 Compose 编排：

```bash
cp .env.example .env        # 填写 POSTGRES_PASSWORD / DATABASE_*（.env 不入库）
docker compose up -d        # 数据库 → 迁移 → Runtime（role=all）
docker compose logs -f runtime
```

**服务组成**

| 服务 | 说明 |
|---|---|
| `timescaledb` | PostgreSQL + TimescaleDB（数据卷持久化） |
| `redis` | 缓存层基础设施（非权威、无持久化，可随时清空重建；默认 2 GB + volatile-lru） |
| `migrate` | 一次性迁移（`upgrade()`），成功后退出 |
| `grant-readonly` | 一次性只读授权（迁移后执行，幂等） |
| `service` | 管理 API / WebUI（FastAPI + SPA；默认仅本机 127.0.0.1:8000） |
| `runtime` | 控制面进程（`--role all`，单机默认） |

**行为约定**

- **项目隔离**：全栈项目名 `fin-data-platform`；开发库项目名
  `fin-data-platform-dev`（端口默认 15432），两者容器与数据卷互不影响；
- **自动迁移**：`runtime` 等待 `migrate` 成功后再启动，避免多角色竞争迁移；
- **拆分角色**（多进程仅凭数据库协调）：先停单机进程，再启动拆分角色——

  ```bash
  docker compose stop runtime
  docker compose --profile split up -d runtime-scheduler runtime-worker
  ```

- **配置注入**：容器内 `DATABASE_HOST` 固定为 `timescaledb`；
  `FDP_SYNC_*` 与 `TUSHARE_TOKEN` 由 `.env` 注入；未配置 `FDP_SYNC_CODES`
  时启动为空 Runtime；
- **变量优先级**：shell 环境变量 > `.env`（本地若曾 `export DATABASE_*`，
  会覆盖 `.env` 注入，排查时注意清理）；
- **健康检查**：镜像内置 `--check`（数据库 / 字典 / schema 版本），
  `docker compose ps` 显示 healthy；
- **日志与资源**：日志滚动上限 10 MiB × 3；数据库 / 缓存 / 迁移与控制面设置
  内存上限，控制面另设 CPU 配额；
- **端口暴露**：数据库端口默认监听所有接口；仅本机访问可设
  `POSTGRES_BIND=127.0.0.1`（开发库为 `DEV_POSTGRES_BIND`）。

**运维命令**

```bash
docker compose ps                      # 状态与健康
docker compose logs -f runtime         # 日志
docker compose restart runtime         # 重启控制面
docker compose down                    # 停止（保留数据卷）
docker compose down -v                 # 停止并清空数据卷（慎用）
```

## 5. 缓存（L1 进程内 + L2 Redis）

缓存**非权威**：清空后可完全由权威层重建；后端异常时 **fail-open**（按 miss 处理，直查权威层），
不阻塞数据链路。

**配置**

| 变量 | 默认 | 说明 |
|---|---|---|
| `FDP_REDIS_URL` | 空 | Redis 连接串（如 `redis://redis:6379/0`）；**未设置则不启用缓存** |
| `FDP_CACHE_TTL` | 21600（6h） | 默认 TTL（秒） |
| `FDP_CACHE_TTL_<DOMAIN>` | — | 按域覆盖（域名大写，如 `FDP_CACHE_TTL_CN_EQUITY`） |
| `FDP_CACHE_L1_ENTRIES` | 4096 | L1 条数上限 |
| `FDP_CACHE_L1_BYTES` | 256 MiB | L1 字节上限 |
| `REDIS_MAXMEMORY` | `2gb` | 编排中 Redis 内存上限（`volatile-lru` 淘汰） |
| `REDIS_BIND` | `127.0.0.1` | 编排中 Redis 端口监听地址（无鉴权，默认仅本机） |

**键与失效**

```
fdh:{domain}:{panel}:g{generation}:{asof:<时间>|kt:<时间>}:{params_hash}
```

- **PIT 安全**：`as_of` 与 `knowledge_time` **互斥且必须二选一**——防止前视与陈旧数据混用；
- **代际失效**：同步成功 → 域代际 +1，旧键自然失效（免 `SCAN`）；
- **防击穿**：L2 锁跨进程 single-flight；等待超时回退自算；
- **序列化**：DataFrame → Arrow IPC；小对象 → JSON；含格式版本（不兼容按 miss 处理）。

**用法**

```python
from fin_data_platform.cache import cache_from_env
from fin_data_platform.storage.readers import cached_frame

cache = cache_from_env()                     # 未配置 FDP_REDIS_URL → None
if cache is not None:
    key = cache.build_key("cn_equity", "daily_bar", as_of="2026-09-14", params={"fields": ["close"]})
    frame = cached_frame(cache, key, loader=lambda: load_from_db())
    stats = cache.stats()                    # 命中率 / 字节 / 淘汰 / 锁等待
```

## 6. Runtime（控制面）

### 6.1 启动

```bash
.venv/bin/python -m fin_data_platform.runtime --role all
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--role` | `all` | `all`（单机）/ `scheduler`（仅调度分发）/ `worker`（仅执行） |
| `--workers` | 2 | 工作线程数 |
| `--log-level` | `INFO` | 日志级别 |

退出码：`0` 正常停止；`1` 启动健康检查未通过；`2` 配置非法。

### 6.2 同步任务（环境变量）

| 变量 | 必填 | 说明 |
|---|---|---|
| `FDP_SYNC_CODES` | ✅ | canonical 代码清单，逗号分隔（如 `600519.SH,000001.SZ`）；未设置时不装配同步任务 |
| `FDP_SYNC_START` | ✅ | 首次窗口起点（ISO 日期 `YYYY-MM-DD`），用于水位缺失时的补数 |
| `FDP_SYNC_SOURCE` | ✅ | 数据源（`tushare` / `akshare` / `ifind` / `wind` / `fuyao` / `baostock`），必须显式指定 |
| `FDP_SYNC_SCHEDULE` | | 5 段 cron（UTC）或 `interval:<秒>`；缺省为轮询追平（启动即补，不设定时） |
| `TUSHARE_TOKEN` | 按需 | 数据源凭证（亦可 `FIN_DATA_HUB_TUSHARE_TOKEN`） |

```bash
export TUSHARE_TOKEN=...
export FDP_SYNC_CODES=600519.SH
export FDP_SYNC_START=2026-09-01
export FDP_SYNC_SOURCE=tushare
export FDP_SYNC_SCHEDULE='0 9 * * 1-5'
.venv/bin/python -m fin_data_platform.runtime --role all
```

行为：启动即从水位追平到最近已收盘交易日，成功后推进水位；失败按运行记录
重试；调度注册持久化，进程重启不丢。

装配：每个代码同时注册**日线**与**复权因子**任务（`sync.cn_equity.daily_bar.<code>`
与 `sync.cn_equity.adj_factor.<code>`），共用 `FDP_SYNC_SCHEDULE` 与首次起点；当前
source 未声明复权因子能力（如 `akshare`）时跳过因子任务并告警。注意复权因子当日
18:00 后才发布（数据字典 `earliest_available`），若调度早于该时点，当日因子留到
下一轮补齐。

### 6.3 运行时参数（`RuntimeConfig`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `role` | `all` | 进程角色 |
| `worker_count` | 2 | 工作线程数 |
| `tick_interval` | 1.0 s | 调度到期计算间隔 |
| `worker_interval` | 0.2 s | 工作池轮询间隔 |
| `max_queued` | 100 | 背压上限（排队 + 重试中） |
| `check_dictionary` | `True` | 启动校验字典 |
| `check_schema` | `True` | 启动校验数据库 schema |

## 7. 管理 API 与 WebUI

面向**平台治理**的 REST 接口（doc-14：WebUI 只经 REST，不直连数据库）：

```bash
.venv/bin/python -m fin_data_platform.api --host 127.0.0.1 --port 8000
# 交互文档：http://127.0.0.1:8000/api/docs
```

| 端点 | 说明 |
|---|---|
| `GET /v1/datasets`、`/v1/datasets/{dataset}` | 数据集字典（字段/口径/PIT/键/SLA/质量/血缘/存储/映射） |
| `GET /v1/entities`、`/v1/entities/{id}`、`/v1/entities/relation-types` | 实体检索与详情（时间轴/代码履历/关系/外部标识） |
| `GET /v1/jobs`、`/v1/jobs/{run_id}`、`/v1/watermarks` | 任务运行记录与数据水位 |
| `POST /v1/jobs/sync` | 触发同步：提交意图到 `meta` 队列（Runtime 执行；幂等键 `request_id`） |
| `GET /healthz` | 健康检查（数据库 / 字典 / schema 版本） |

**连接口径**：数据读取（数据集/实体）使用 `read_dsn`（只读角色）；控制面（任务/水位/触发）
使用写连接，且只写 `meta` 意图——采集由 Runtime 执行，不绕过控制面。

**触发语义**：`request_id` 重复提交返回既有运行（幂等）；同窗口（`job_key`）重复提交被
幂等忽略；`end` 不得晚于今天（UTC）。调度侧按交易日历判定"已收盘/已发布"，
盘中手工触发可能拿到未完成的当日数据，请谨慎。

**部署**：compose 中的 `service` 默认绑定 `127.0.0.1:8000`（`API_BIND`/`API_PORT` 可调）；
WebUI 静态资源目录由 `FDP_WEB_DIST` 指定（缺省 `web/dist`），由同一服务托管并
SPA 回退到 `index.html`。

## 8. 测试用环境变量

集成测试读取（库本身不读取这些变量，仅测试使用）：

| 变量 | 用途 |
|---|---|
| `FIN_DATA_HUB_TUSHARE_TOKEN` | Tushare 集成测试 |
| `FIN_DATA_HUB_WIND_API_KEY` | Wind 集成测试 |
| `FIN_DATA_HUB_IFIND_TOKEN` | iFinD 集成测试 |
| `FIN_DATA_HUB_FUYAO_API_KEY` | Fuyao 集成测试 |
| `DATABASE_*` / `FDP_DATABASE_HOST` | 存储集成测试 |
| `FDP_TEST_DATABASE=1` | 允许破坏性迁移测试 |

```bash
.venv/bin/python -m pytest                    # 离线单测（默认跳过集成）
.venv/bin/python -m pytest -m integration     # 端到端（需凭证 / 数据库）
```

## 容器出网代理（可选）

数据同步在容器内发起；若宿主网络直连被拦、必须经本地代理出网（如 Clash 等监听
`127.0.0.1` 的代理），需把代理注入应用容器——容器内的宿主名用 `host.docker.internal`
（Docker Desktop 网关），数据库/Redis 等内网服务必须列入 `NO_PROXY`：

```bash
# .env（不入库）
FDP_CONTAINER_PROXY=http://host.docker.internal:7897
FDP_CONTAINER_NO_PROXY=timescaledb,redis,localhost,127.0.0.1
```

排查顺序：容器内 `getent hosts api.tushare.pro`（DNS）→ TCP 连通性 →
带代理的 HTTPS 请求。若代理仅监听 `127.0.0.1`，容器需经 `host.docker.internal`
访问；直连失败的典型症状是 TLS 握手被中断（`UNEXPECTED_EOF`），且部分 SDK 会
把异常吞掉返回空表（表现为"同步成功但 0 行"）。
