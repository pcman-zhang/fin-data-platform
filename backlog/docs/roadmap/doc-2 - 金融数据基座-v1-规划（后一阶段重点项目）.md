---
id: doc-2
title: 金融数据基座 v1 规划（后一阶段重点项目）
type: guide
created_date: '2026-09-13 05:58'
updated_date: '2026-09-27 12:19'
---
# 金融数据基座 v1（后一阶段重点项目）规划草案

> 状态：立项记录（待设计） | 方向：将 v0 采集库演进为「带数据库的全域金融数据平台」

## 1. 定位

从「进程内取数库」升级为**可独立部署的金融数据基座**：多源采集 → 持久化 → 数据字典/血缘 → 数据质量 → 统一对外服务。

v0 资产直接复用：多源适配器、统一 WindCode、限流/计量/缓存、capability 驱动的请求合并；v1 在其上叠加存储、调度与服务化层。

## 2. 核心工作流

1. **数据字典与血缘**：每个数据项记录来源、更新频率、覆盖范围、约束与注意事项、单位、时区；派生指标记录计算公式与依赖关系。
2. **PostgreSQL 存储层**：企业级数据库（TimescaleDB 扩展）；schema 设计（标的/行情/财务/宏观/特色数据等）、分区、增量写入、幂等、保留策略、迁移管理。
3. **独立部署（Docker）**：镜像与编排、配置/凭证注入（不落镜像）、健康检查、资源限制、内网/离线部署。
4. **数据质量检查**：完整性、唯一性、时效性（延迟）、跨源对账（复用 v0 对账框架）、异常值与跳变检测、质量报告与告警。
5. **采集调度与增量同步（新增）**：定时/增量任务、失败重试与补数、任务可观测；与库解耦，作为服务进程运行。
6. **对外服务**：FinDataPlatform SDK 优先（直连读模型 + PIT），REST API / WebUI / 批量导出为薄封装；权限与审计。

## 3. 与 v0 的关系

- v0（fin-data-hub 库）作为采集内核保留；v1 以新增包/服务方式叠加，不破坏现有 API。
- 「无持久化存储层」约束仅在 v0 阶段有效；v1 明确引入 PostgreSQL，`AGENTS.md` 与架构文档需标注阶段边界。

## 4. 验收方向（高层）

- 可执行的数据字典（含派生指标公式）覆盖首批数据域；
- FinDataPlatform SDK（直连读模型 + PIT/as-of）与 REST 薄封装可用，语义一致；
- PostgreSQL + TimescaleDB 可幂等重建全量数据，增量同步可重复执行；
- `docker compose up` 一键起服务（含数据库、Redis 与管理 WebUI），配置注入可复现；
- 质量检查每日产出报告，关键指标对账通过率 100%；
- Redis L2 缓存跨进程命中可观测，同步后失效正确，缓存键含 PIT 语义（无前视）；
- 只读副本 + 稳定读模型（PIT 安全）对外可用；外部不直连内部原始表；
- 批量导出 Parquet/Arrow 可用；小批量在线查询 P95 ≤ 200–500ms；
- 管理型 WebUI 覆盖数据域/同步状态/质量报告/字典浏览/任务与配额；
- SDK 版本与 DB schema 版本兼容矩阵落地。

## 5. 开放问题

1. ~~仓库形态~~ 已定：**同仓库**；分层目录为 `src/fin_data_hub` + `src/fin_data_platform` + `services/*` + `docker/`（见 §6.10）。
2. ~~数据域优先级~~ 已定：以 Fuyao 平台能力范围为准（A股行情/财务/估值/资金流/特色数据、基金、期货、期权）+ **保留宏观数据平面**（见 §6.8）。
3. ~~数据库与部署~~ 已定：PostgreSQL + TimescaleDB + Redis；**单机 Docker（compose），暂不需要 K8s**（见 §6.8）。
4. ~~调度实现~~ 已定：APScheduler（PostgreSQL job store）进程内调度，详见 §6.1。
5. ~~服务接口~~ 已定：SDK 优先（FinDataPlatform），REST 薄封装 + 管理 WebUI；MCP 作为可选补充。
6. ~~历史回填~~ 已定：由用户自行下载历史数据文件；平台提供 Parquet/CSV 幂等导入通道（见 §6.8）。
7. ~~下游迁移~~ 暂不在本期考虑；后续在下游系统继续开发（见 §6.8）。
8. ~~PIT 保留策略~~ 已定（业内标准）：事件/知识类数据双时间轴 + append-only 全量版本（as-first-reported / as-restated）+ `is_latest` 物化视图；行情类天然 PIT，复权因子版本化（见 §6.9）。
9. ~~WebUI 技术栈与角色~~ 已定：后置，待系统建设到一定程度后再考虑。
10. ~~BaoStock 等新增源~~ 已定：暂不考虑；hub 架构定稿后作为 DataSource/Adapter 扩展点接入，成本低。
11. ~~Redis 细节~~ 已定：单机 **2GB 上限** + `volatile-lru`；纯缓存可关持久化；失效用**代际版本号**（同步后 INCR）；值序列化 Arrow IPC（小对象 JSON）；Redis 故障 fail-open 直查；观测命中率/内存/淘汰/锁等待（见 §6.2）。
12. ~~只读副本~~ 已定：暂不考虑（尚非商业部署）；本期仅保留**读写 DSN 分离配置**（DevOps/接入点配置），未来拆分只改配置（见 §6.11）。

## 6. 架构决策

- **对外接口**：① `FinDataHub`（多源接入平面，仅平台内部消费）；② `FinDataPlatform` SDK（直连只读副本/读模型，原生 PIT/as-of）+ 薄 REST（对外消费；WebUI 基于 REST）。
- **内部组织**：`DataPanel`（数据平面）组织各数据域；**PIT（point-in-time）为强制语义**——记录事件时间与知识时间（发布/入库），支持 as-of 查询、重述与防前视。
- **数据源扩展**：DataSource/Adapter 插件化（Tushare / AkShare / Wind / iFinD / Fuyao / BaoStock 等），复用 v0 的 capability、限流、计量与缓存；新源接入不修改平台核心。
- **数据库**：PostgreSQL + **TimescaleDB**（hypertable、连续聚合、压缩与保留策略），满足时序性要求。
- **管理面**：管理型 WebUI（数据域与同步状态、质量报告、数据字典浏览、任务与配额/成本）。
- **缓存层**：**Redis 作为平台级共享缓存（L2）**，详见 §6.2。

建议目录（同仓库方案，已定）：

```
src/
  fin_data_hub/          # ① FinDataHub：多源接入（适配器/限流/计量/缓存）；唯一数据源访问平面
  fin_data_platform/     # ② FinDataPlatform SDK：DataPanel(PIT)/storage/quality（复用 FinDataHub）
services/
  api/                   # ③ 薄 REST（基于 SDK；鉴权/审计/缓存/压缩/Arrow）
  scheduler/             #    采集调度（复用 FinDataHub + 平台存储）
  webui/                 # ④ 管理台（基于 REST）
docker/                  # 镜像与 compose（单机）
```

### 6.1 调度方案（已定：APScheduler）

- **一期：APScheduler 进程内调度**（`platform/services/scheduler/`），job store 用 PostgreSQL（重启不丢任务）；单副本直接运行，多副本用 PG advisory lock 防重。理由：不引入额外中间件（compose 单机即可），与 PostgreSQL 已有组件复用。
- **任务设计**：
  - 交易日历感知（复用 v0 `get_trade_calendar`）；典型任务：盘后日线、每日快照/资金、参考数据、财务/持仓（按披露节奏）、质量检查（同步后链式触发）、回填（手动/一次性）。
  - **执行语义**：任务幂等（upsert + 运行记录）；失败退避重试复用 v0 `retry_call`；限流/配额复用 v0；断点补数按窗口重跑。
  - **可观测**：`job_runs` 表（job/窗口/状态/耗时/行数/错误/request_id）供 WebUI 与告警使用；可选 Prometheus 指标。
  - **配置**：数据域 YAML（域、源优先级、cron、窗口、依赖）挂载或入库。
- **演进**：抽象 `Scheduler` 接口；任务图复杂化或需分布式时，可替换为 Dagster/Prefect（管道 UI、回填、血缘）或 K8s CronJob；一期不引入 Celery/Airflow 级重组件。
- **边界**：调度只属于 `platform/`，不进 v0 库（库保持无调度、无存储）。

### 6.2 缓存层（Redis，已定）

- **定位**：平台级共享缓存（**L2**）；v0 库内 `MemoryCache` 保持进程内 **L1**，不引入外部服务依赖。
- **用途**：查询结果跨进程共享（API / 调度 / Worker）、可选分布式锁（调度防重）、可选配额/限流计数（跨进程聚合）。
- **键设计**：`fdh:{domain}:{panel}:{as_of|knowledge_time}:{params_hash}`；**PIT 语义必须入键**（避免前视与陈旧数据混用）；值序列化优先 Arrow/Parquet 字节或 JSON，附 schema 版本。
- **TTL 与失效**：按域分级 TTL（参考数据长、行情短）；同步任务完成后按域/标的主动失效；数据字典/交易日历长 TTL。
- **防击穿**：Redis 锁 + 短 TTL 占位实现**跨进程 single-flight**（与 v0 进程内 single-flight 呼应）。
- **部署**：docker compose 增加 Redis 服务；凭证注入不落镜像；按缓存定位可关闭持久化。
- **边界**：Redis 仅属 `platform/`；v0 库保持内存缓存、无外部服务依赖。

### 6.3 DuckDB 定位（已定：分析/加工引擎，非缓存）

- **角色区分**：Redis 是**缓存**（L2：TTL/失效/锁/计数）；DuckDB 是**嵌入式分析引擎**（列式 OLAP），两者不互替。
- **DuckDB 用途**：
  - 处理 Parquet 全市场数据（如行情 dump）后批量入库（ETL 加工）；
  - 派生指标/面板的批量计算与研究查询（回测式 SQL）；
  - 数据质量扫描与跨源对账的离线计算；
  - 可选：导出只读研究快照（Parquet/DuckDB 文件）供下游分析。
- **不作为**：平台共享缓存（无 TTL/失效语义、单写多读、跨容器文件一致性差）；也不作为真源（真源是 PostgreSQL + TimescaleDB）。
- **部署**：以进程内库使用（`storage/` 或 `quality/` 内部），无需独立服务；如导出研究快照，走对象存储/共享卷。
- **结论**：**缓存选 Redis，分析加工用 DuckDB**；若仅能保留其一，缓存角色保留 Redis（DuckDB 仅在本地研究/加工场景不可替代）。

### 6.4 外部量化消费与性能策略（建议）

- **结论**：REST 适合**在线/小批量/低频**查询；**不适合作为回测大面板的唯一通道**。采用「REST 在线 + 批量导出 + 可选只读副本」混合访问模型。
- **量级参考**：LAN 上 REST+JSON 单请求开销约 1–10ms；但 JSON 体积与解析成本高（全市场 10 年日线可达 GB 级），且逐标的拉取受请求数与限流约束。
- **分工**：
  1. REST 在线查询：快照、参考数据、PIT/as-of 查询、小批量行情/财务、控制面；
  2. 批量导出：Parquet/Arrow 异步任务（对象存储/共享卷），供回测与研究；
  3. 可选只读副本：PostgreSQL/TimescaleDB 只读副本或 ADBC/Arrow SQL，服务重度查询；
  4. 量化侧本地增量物化（DuckDB/Parquet），回测不走网络。
- **REST 增强项**：gzip/zstd 压缩、字段裁剪、游标分页、`Accept: application/vnd.apache.arrow.stream` 列式响应、ETag/If-None-Match、HTTP/2 与多副本。
- **SLO 建议**：小批量在线查询 P95 ≤ 200–500ms；全市场/长历史一律走批量导出（异步任务 + 下载）。
- **判断规则**：日频/小时频策略与交互研究 REST 足够；分钟级以下或大规模面板回测需批量导出/只读副本。

### 6.5 外部直接使用数据库（已定：只读副本 + 稳定读模型）

- **可以**：外部量化平台可直接连库，但**只连只读副本上的稳定读模型**（如 `mart` / `api` schema 的视图与物化视图），**不直连内部原始表**。
- **理由**：直连内部表会绕过权限/审计/限流/成本计量、PIT 语义与质量门，并把外部系统耦合到内部 schema 演进上。
- **读模型要求**：版本化 + 数据字典登记；PIT/as-of 视图（保证无前视）；字段与口径稳定，变更走弃用流程；跨源合并与派生指标在平台侧完成。
- **权限与隔离**：按域只读角色（最小权限）；独立副本 + `statement_timeout` + 连接数限制，避免分析负载冲击写入主库；可叠加 pgAudit 审计。
- **访问方式优先级**：批量导出（回测）＞ 只读副本/Arrow SQL（即席分析）＞ REST（在线查询与控制面）。
- **只读副本的用途**：
  1. **负载隔离**：外部即席分析、批量导出、质量扫描走副本，避免与主库写入（采集 upsert）争抢资源；
  2. **安全边界**：对外只暴露副本（只读角色 + 网络隔离），主库不对外；
  3. **读模型承载**：`mart`/`api` 视图与物化视图可按分析负载独立优化（索引/资源）；
  4. **运维收益**：备份分流、作为 HA 故障切换基础；
  5. **可选扩展**：副本上启用分析类扩展或放宽 `statement_timeout`，不影响主库。
- **分期**：一期 compose 可先用同实例只读角色 + 读模型；当外部重度分析或 HA 需求出现时再拆独立只读副本。

### 6.6 SDK 优先，REST 薄封装（已定）

- **定位**：`FinDataPlatform` 本体是 **Python SDK**（直连只读副本/读模型，原生 PIT/as-of 参数与元数据）；REST API、WebUI、批量导出均调用同一 SDK/核心，不重复实现语义。
- **收益**：PIT/口径/质量标记单点实现（SDK 与 REST 不分裂）；量化侧零 HTTP/JSON 开销，可直接返回 Arrow/DataFrame；SDK 可 pip 安装，服务化成为可选部署形态。
- **访问与治理**：SDK 走**只读副本 + 读模型**（禁止内部原始表），连接配置注入；REST 在 SDK 之上叠加鉴权/审计/限流/Redis 缓存/压缩与 Arrow 响应。
- **版本与兼容**：SDK 语义化版本 ↔ DB schema 版本**兼容矩阵**；连接时校验 schema 版本，不兼容给出明确错误；读模型弃用流程与 SDK 版本同步。
- **分发**：SDK 与 REST 同仓库、独立发行物（独立包名或 extras）；服务镜像内置同版本 SDK。
- **PIT 契约**：`as_of` / `knowledge_time` 为 SDK 查询一等参数；结果附带 `source / as_of / quality` 元数据。

### 6.7 凭证与权限模型（已定：三个凭证面）

- **写入面（平台内部）**：ingestion、派生计算、文件导入、质量结果等内部服务持有主库**读写**凭证（按 schema 最小授权）；不对 SDK / REST 暴露。
- **SDK 面（外部直连）**：PostgreSQL **只读角色**（连只读副本），按域授权（`mart` / `api` schema SELECT）；凭证为 **DB 凭证**（账号密码 / IAM / 证书），非 API Key；按消费方独立账号并轮换。
- **REST 面（外部服务化）**：**API Key**（哈希存储、按消费方签发、可撤销），作用域 `read` / `export` / `admin`（触发同步/回填等管理动作）；**不提供数据写入**。
- **WebUI**：管理员会话（SSO 或本地账号）+ 角色（只读 / 运维），独立于对外 API Key。
- **原则**：数据写入只经平台内部服务（采集 / 派生计算 / 文件导入 / 质量结果；幂等 + 质量门 + PIT 知识时间）；外部写入会破坏治理与 PIT 语义。如需用户自有数据，另设命名空间/表，不进入平台数据域。
- **SDK 双模式**：SDK 支持「直连 DB（只读凭证）」与「REST 后端（API Key）」两种模式，供有无 DB 网络权限的消费方选择，语义一致。
- **凭证卫生**：不落仓库/镜像/日志；TLS；最小权限；轮换；按 key/角色限流与成本归因；审计留痕。

### 6.8 范围与部署决策（2026-09-13）

- **数据域优先级**：以 Fuyao 平台能力范围为准（A股行情/财务/估值/资金流/特色数据、基金、期货、期权），**保留宏观数据平面**（EDB 类指标）。
- **市场平面**：创业板不是独立市场（属 A 股深市板块）；建议**同一权益数据平面**内以 `market`（CN/HK/US）+ `exchange` + `board` 维度区分，统一 schema/PIT/接口，差异（交易日历、币种、复权、字段可用性）参数化；H股/美股为后续扩展（Fuyao 一期不覆盖，可由 Wind/iFinD 等补充）。
- **部署**：单机 Docker（compose），暂不需要 K8s。
- **历史回填**：由用户自行下载历史数据文件；平台提供**文件导入通道**（Parquet/CSV 幂等入库），不承担付费源大规模回填成本。
- **下游**：暂不在本期考虑；后续在下游系统继续开发。

### 6.9 PIT 分级与标准做法（2026-09-13）

- **分级**：
  - **行情/快照类**：天然 PIT（发布后不变）；无需版本化，知识时间≈事件时间。
  - **公司行为/复权因子**：版本化；复权价**按 as-of 基准日计算**（原始价 + 因子，不落全量 qfq 快照）。
  - **财务/财务指标/宏观 EDB**：存在修订/重述 → append-only 版本（as-first-reported / as-restated）。
  - **指数成分/权重**：生效区间（in_date / out_date）+ SCD2 维度。
  - **公告/新闻**：知识时间 = 发布时间（datetime 精度）。
  - **基金持仓/份额**：披露滞后，知识时间 = 公告日。
- **标准字段（四时间模型，详见 doc-10 §4）**：`event_time`（事件时间，含 `report_period`）、`publish_time`（官方发布时间）、`knowledge_time`（平台获知时间）、`ingest_time`（入库时间）、`version`、`is_latest`、`source`。
- **查询语义**：`as_of` 过滤 `knowledge_date <= as_of` 后取每键最新版本；不传 `as_of` 即"当前最新"；更正不回写历史（append-only）。
- **PIT 宇宙（实体注册表（Entity Registry）必须含退市标的）**：`as_of` 时点标的池 =
  `list_date <= as_of AND (delist_date IS NULL OR delist_date > as_of)`；
  - 实体注册表（Entity Registry）必须覆盖 `L / P / D` 全部状态（含已退市），并保留 `list_date / delist_date`；
  - **退市标的的行情/财务/分红/停牌等历史数据必须完整入库**（否则回测/成分还原存在幸存者偏差）；
  - 名称变更（`namechange`）、ST 历史（`stock_st`）、停牌（`suspend_d`）、行业归属变更
    （`index_member_all` in/out）按**生效区间**独立留痕，用于 as-of 时点还原；
  - v0 已具备：`delist_list`（TASK-2.26）、停牌/ST 计划（TASK-2.29）、行业成分带 in/out（TASK-2.27）；
    仍缺：名称变更历史（`namechange`）——需纳入事件接口或参考数据。
- **成分/权重表也是 PIT**（防前视/幸存者偏差）：
  - **区间型（成员进出）**：`in_date / out_date`（SCD2）；as-of 成分 =
    `in_date <= as_of AND (out_date IS NULL OR out_date > as_of)`；申万行业成分（TASK-2.27）、
    指数调整公告属此类；
  - **快照型（定期权重）**：`trade_date <= as_of` 取**最近一期**（`index_weight` 月度，TASK-2.30）；
  - **知识时间**：以公告/披露时间为准（生效日与公布日分离时用公布日 ≤ as_of，防前视）；
  - 仅存快照、无历史区间的指数成分，可用月度快照差分补区间（需数据质量校验/对账）。
- **保留策略**：全量保留 + `is_latest` 物化视图（存储成本可接受；对账/审计友好）。
- **参照**：bitemporal（valid time + transaction time）、SCD2/6（Kimball）、Compustat/CRSP PIT 口径（as-first-reported vs as-restated）。

### 6.10 系统分层与目录（2026-09-13 决策）

分层（依赖方向自上而下；禁止反向依赖或跨层直连）：

1. **`src/fin_data_hub/`（① 多源接入平面）**：DataSource/Adapter 插件（Tushare/AkShare/Wind/iFinD/Fuyao/BaoStock…）+ 限流/计量/缓存；对外接口 `FinDataHub`；**平台及任何下游只经此平面访问数据源**，不直连厂商 SDK。
2. **`src/fin_data_platform/`（② FinDataPlatform SDK）**：DataPanel（数据域/PIT）、TimescaleDB 存储、质量、读模型；消费 ①；独立发行物（可 pip）。
3. **`services/api/`（③ 薄 REST）**：基于 SDK，不重复实现语义；鉴权/审计/缓存/压缩/Arrow。
4. **`services/webui/`（④ 管理台）**：基于 REST API（不直连 DB/SDK）。

- `services/scheduler/`：采集调度，复用 ①（取数）与 ②（存储/质量）。
- `docker/`：镜像与 compose（单机）。
- **命名**：v0 门面由 `DataHub` 更名为 `FinDataHub`（保留 `DataHub` 兼容别名并标注弃用）。

### 6.11 市场平面与 Redis/副本定稿（2026-09-13）

**市场平面（已定：统一平台、市场分平面）**

- **每市场独立数据子平面**（各自 Normalization）：`cn_equity`（沪深北 + 主板/创业板/科创板 board 维度）、`cn_fund`、`cn_futures`、`cn_options`、`hk_equity`、`us_equity`、`us_options`；按需扩展。
- **差异由子平面承担**：代码格式（CN 6 位 / HK 5 位 / US ticker）、字段与粒度、单位（手/股）、交易日历、币种、复权与公司行为、交易规则（如 CN 无 PUT、T+1）。
- **共享层**：实体注册表（Entity Registry，跨市场实体与代码履历/退市）、PIT 框架、数据字典、质量框架、存储基础设施与统一 SDK 接口（panel 参数）。
- **宏观平面单一**：EDB 类指标（利率/收益率/资金/大宗/汇率官方序列）；汇率建议归宏观（低频官方/收盘序列）；若未来做交易级 FX 行情再单设 FX 平面。
- **归属按数据形态而非标的名**：TLT/SHY 作为 ETF 价格属美股权益平面；EDB 中的美债收益率序列属宏观平面。
- **代码模型扩展**：HK（5 位数字 `.HK`）、US（字母 ticker，`.O`/`.N`/`.US` 映射待定）需在 `SecCode`/映射层扩展，纳入 `TASK-3.1` 设计。

**Redis（定稿）**：单机 2GB + `volatile-lru`；纯缓存可关持久化；代际版本失效；Arrow IPC 序列化；fail-open 降级；观测指标入 WebUI/告警。

**只读副本（定稿）**：暂不考虑；保留 `DB_WRITE_DSN` / `DB_READ_DSN` 分离配置，作为 DevOps/接入点配置项。

### 6.12 指数子平面与归一化层（2026-09-13 决策）

- **指数子平面**：`cn_index`（交易所/中证国证：`.SH`/`.SZ`/`.CSI`）、`hk_index`（`.HK`，如 `HSI.HK`）、`us_index`（`.GI`，Wind 全球指数口径，如 `SPX.GI`）、`ths_index`（同花顺自定义，`.TI`）、`wind_index`（Wind 自定义，`.WI`）；自定义指数按 provider 独立子平面。
- **代码标准**：canonical **遵循 Wind 标准**（`.SH`/`.SZ`/`.BJ`/`.OF`/`.HK`/`.O`/`.N`/`.A`/`.GI`/`.TI`/`.WI`/`.CSI`）；数据源不支持时通过**映射校准**（不改变 canonical）。
- **归一化层（Normalization Layer）**：位于 `fin_data_hub`（源侧），职责：
  1. 请求代码转换：canonical → 源端代码（按 source × endpoint）；
  2. 请求参数转换：日期格式、复权枚举、市场后缀、endpoint 参数形态；
  3. 响应归一化：字段/单位/枚举/时区/币种/日期 → hub 统一 schema + 元数据。
- **实现方式**：映射 **spec 驱动**（机读文件，source × endpoint），与平台数据字典（TASK-3.2）校验一致；CI 检查覆盖度。
- **边界**：hub 做"源 → 统一"归一化；platform 做"面板级"归一化（跨源合并、PIT 版本、派生指标、质量门）。

### 6.13 数据库读写模块设计（2026-09-13 方案）

**模块划分（`src/fin_data_platform/storage/`）**

- `engine.py`：连接与引擎管理——`DB_WRITE_DSN`（平台内部写入端：ingestion / 派生计算 / 导入 / 质量）与 `DB_READ_DSN`（SDK/REST）分离；连接池、`statement_timeout`（读）、TLS。
- `schema.py` + `migrations/`：SQLAlchemy Core 表定义；Alembic 迁移 + Timescale DDL（hypertable、压缩、保留、连续聚合）；`schema_version` 表。
- `writers.py`：幂等写入（staging + COPY → merge upsert；唯一键 `ON CONFLICT`）；PIT append-only（重述追加版本，不回写历史）；批次事务 + advisory lock 防并发重复。
- `readers.py`：as-of 查询（`knowledge_date <= as_of` + 每键取最新版本）、时间序列/截面查询、游标分页、字段投影、Arrow 输出（ADBC/psycopg）。
- `versioning.py`：schema 版本与 SDK 兼容校验（连接时检查，不兼容明确报错）。

**写入路径（平台内部写入端：ingestion / 派生计算 / 文件导入 / 质量结果）**

- 写凭证仅平台内部服务持有，按 schema 最小授权：ingestion 写 raw/staging 与主数据；派生计算写 derived schema；文件导入写 raw；质量检查写 quality schema。
1. 批次开始：`job_runs` 记录 + advisory lock（按 panel/分区）。
2. 采集结果 → staging（unlogged 临时表）→ `COPY` 批量装载。
3. merge 进 hypertable：幂等 upsert；PIT 数据 append-only（新 `knowledge_date`/`version`/`ingest_ts`）。
4. 派生数据：读取 as-of 正确的输入 → DuckDB/批量计算 → 写入 derived 表并记录血缘（输入数据集版本、公式版本、computed_at）；源数据重述时重算并追加版本。
5. 提交后：刷新 `is_latest` 物化视图 / 连续聚合；递增 Redis 代际版本触发缓存失效。
6. 失败：批次回滚 + 重试（复用 v0 退避）；重跑结果一致。

**读取路径（SDK 直连 / REST 经 SDK）**

- as-of 语义：`DISTINCT ON (key) ... ORDER BY knowledge_date DESC`（或窗口函数），保证无前视；不传 `as_of` 即当前最新。
- 读模型：`mart` / `api` schema 视图与物化视图（对外稳定契约）；SDK 内部查询同样走读模型，不碰内部原始表。
- 缓存：读经 Redis L2（键含 panel/as_of/params；代际失效）；写路径不经过缓存。

**技术选型**：SQLAlchemy 2.x Core + Alembic + psycopg3；批量导出/读取用 Arrow（ADBC 或 psycopg Arrow）；SDK 同步实现，REST 以线程池调用同一 SDK（不维护双份实现）。

**测试**：临时 PostgreSQL（testcontainers 或 compose profile）跑迁移/幂等重跑/as-of/性能基准；CI 中不依赖生产库。

### 6.14 时序能力与双时间轴（2026-09-13 决策）

- **模型**：数据平面 = **双时间轴时序**——事件时间轴（event_time，序列观测时点：交易日/报告期/指标期数）与知识时间轴（knowledge_time，PIT 版本/发布轴）；查询同时指定 `start/end`（事件时间范围）与 `as_of`（知识时间），二者正交。
- **时序能力清单**：
  1. 范围序列查询：多键 × 字段 × 时间范围，频率参数（1d/1w/1mo/1q/1y 与分钟级）；
  2. 日历/时区/时段：交易日历 vs 自然日；交易所时区与会话（CN Asia/Shanghai；HK/US 各自）；
  3. 重采样与聚合：连续聚合（日→周/月），降采样/升采样规则明确；
  4. 缺口处理：null / 前值填充 / 最近值，显式策略并标记；
  5. 窗口与滚动：rolling / 累积 / 同比环比，且 PIT 正确（只用 as_of 可见数据）；
  6. 跨序列对齐与 asof join（如估值序列 join 价格序列）；
  7. 版本查询：vintage 序列（as-first-reported）、同一 event_time 的版本历史；
  8. 多频段：日频与分钟级共存；分钟级按保留策略压缩/降采样。
- **存储支撑（TimescaleDB）**：hypertable 按 (panel, key, event_time) 分区；knowledge_time 为版本维度；按频率建连续聚合；`time_bucket_gapfill` 处理缺口；压缩/保留按频率分级。
- **实现落点（TASK-3.13）**：PIT 要求**先按 `as_of` 过滤再聚合**，故重采样/缺口/窗口在查询层（pandas）完成；TimescaleDB 连续聚合与 `time_bucket_gapfill` 仅用于**非 PIT** 读模型与性能优化，不参与 as-of 查询；分钟级/高频透传为未来特性（§6.15）。
- **SDK 接口草图**：
  - `get_series(keys, fields, start, end, freq="1d", as_of=None, fill=None, calendar="trading")`
  - `get_cross_section(date, as_of=None, fields=...)`
  - `get_versions(key, event_time)`（vintage 历史）
  - `get_panel(keys, fields, start, end, freq, as_of)`（宽表/长表可选）
- **PIT 与派生**：派生序列（收益率/均线/因子）在 as-of 输入上计算并记录血缘（TASK-3.12）。

### 6.15 高频数据透传与延迟统计（未来特性，暂不开发）

- **状态**：登记为未来特性，暂不开发（2026-09-13）。
- **透传（不入库）**：高频数据（分钟 / tick / 实时快照）**不落库、无 PIT 版本**；请求经归一化层后直接返回（hub → SDK/REST）。复用：限流/配额/鉴权；绕过：Redis 缓存（或仅秒级微缓存）与持久化/质量门。
- **归一化仍适用**：代码、字段、单位、时间戳（毫秒精度/时区）、会话状态；不产生知识时间版本。
- **延迟统计**：端到端采样（源调用 → 归一化 → 返回），按 source/endpoint/时间窗输出 **p99 / mean**（可扩展 p50/p95）；复用 v0 UsageLedger 的 latency 记录并扩展分位数；接入 WebUI/指标与阈值告警。
- **边界**：高频透传不改变"行情类入库、事件类版本化"的主线；若未来高频需入库，另立设计与保留策略。

### 6.16 复权口径：raw + factor（2026-09-13 决策）

- **实测确认**：Fuyao `historical` 的 `adjust` 返回**预计算复权价**；因子需从 `corporate-actions` 事件（`dividend_per_share` / `per_share_bonus`）推导；Tushare 提供 `adj_factor` 可直接使用。
- **平台口径**：行情表存**原始价**；单独存**复权因子/公司行为事件**（版本化、PIT）；复权价在读取时按 `as-of` 与请求的 `adjust` 计算，不落全量 qfq/hfq 快照。
- **推导与对账（约定）**：事件乘数 `m = P_prev(1+B)/(P_prev−D)`（P_prev 为除权前收盘原始价）；累计因子需**统一锚点**（建议上市日或首个事件），qfq 仅需相对值；Fuyao 事件分红已与 Tushare 因子隐含分红一致（doc-4），须**逐事件对账**后启用；纳入对账框架（TASK-2.13/3.5）。
- **因子覆盖（实测 2026-09-13，详见 doc-8 §3.1）**：Tushare 股票走 `adj_factor`、ETF/LOF 走 `fund_adj`；BaoStock 仅股票；场外基金/指数无因子。
- **Hub 接口**：`TushareAdapter.fetch_adjust_factors`（`adj_factor` / 待扩展 `fund_adj`，Router 因子源）；`FuyaoAdapter.fetch_adjustment_events`（事件流，推导/对账用，**不注册为 Router factor_source**）。
- **Hub 处置（2026-09-13）**：Fuyao 适配器不提供原生复权；**Router 层**按策略组合 raw（请求源）+ factor（默认 Tushare）合成复权价，可信源走原生复权（doc-5、TASK-2.21）。
- **对账结论（2026-09-13 实测）**：原始价两源完全一致；Tushare `adj_factor` 与理论乘数一致；**Fuyao 预计算复权价不符合"原始价 × 统一因子"语义**（同日 OHLC 比值不一致、内部比例被改变、隐含因子日常波动），**不可作为复权对账基准**；平台以原始价 + 因子为准，Fuyao 事件推导需逐事件与 Tushare 对账。证据见 doc-3 §5。

### 6.17 接口模型与校验：Pydantic（v1 / REST）

- **范围**：`FinDataPlatform` SDK 的请求/响应/配置模型、结果元数据模型；REST（FastAPI）复用同一批模型生成 OpenAPI；JSON Schema 供接口文档与数据字典使用。
- **边界**：v0 `fin_data_hub` 不引入 Pydantic（核心依赖保持仅 `pandas`；契约靠类型 + doc-6 + 显式校验 + 金样测试）。
- **收益**：SDK 与 REST 语义单点定义；跨语言消费方可用 OpenAPI/JSON Schema 生成客户端；校验错误统一。
