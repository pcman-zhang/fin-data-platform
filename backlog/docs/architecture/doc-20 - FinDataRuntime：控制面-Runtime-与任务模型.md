---
id: doc-20
title: FinDataRuntime：控制面 Runtime 与任务模型
type: specification
created_date: '2026-09-14 11:54'
updated_date: '2026-09-20 07:29'
---
# FinDataRuntime：控制面 Runtime 与任务模型

> 状态：**已定稿（第 4 稿）** | 关联：doc-2 §6.1/§6.13（调度与写入）、doc-10 §1/§2/§3.4/§3.5、doc-11（字典）、doc-13 §1/§3.4、TASK-3.4 / 3.5 / 3.6 / 3.8 / 3.9 / 3.12
> 第 4 稿变更：幂等键改为「按 kind 的版本维度」——只在 Read Model Builder（读模型 `semantic_version`）与 Derived Engine（`algorithm_id`，算法语义版本）两处做版本感知；**不引入字典通用版本概念**。
> 第 3 稿变更：评审决策落定——APScheduler job store 保留（运行状态权威仍在 `meta.job_runs`）；Runtime 与 REST 分进程同镜像；字典不做热加载；Export 产物落本地卷；Quality 失败不阻断读模型构建。
> 第 2 稿变更：① Scheduler / Dispatcher / WorkerPool **逻辑分层**；② 新增 **Dependency Manager** 与 `meta.job_dependencies`；③ 隔离原则：任一角色卡死不得阻塞其他角色。
> 目的：为「数据同步 / 调度 / 派生 / 读模型构建 / 缓存 / 元数据」提供唯一常驻宿主（控制面执行体），冻结进程模型与任务模型。

## 1. 定位与边界

**FinDataRuntime（下称 Runtime）是平台控制面的执行体**：常驻进程，负责「何时执行、以什么输入执行、结果落哪、状态记哪」。

- 唯一内部写入面（doc-10 §2）：采集入库 / 回填 / 导入 / 派生计算 / 质量结果 / 读模型构建；
- **不承载数据语义**：字段/口径/单位/血缘以数据字典为唯一契约；Runtime 不新增、不改写语义；
- **不是消费入口**：REST / SDK / MCP 只读 `mart`，它们不是 Runtime 的职责（同镜像部署 ≠ 同职责）；
- 平台核心（Dictionary / Entity Registry / Storage / Derived Engine 逻辑）仍以库与数据库形态存在，Runtime 只是**宿主与编排**；
- 内部按角色分层（Scheduler / Dispatcher / WorkerPool + Dependency Manager），v1 同进程运行、职责先拆，后续可拆进程而不重构。

```
Dictionary（契约）──► Runtime（执行）──► Registry / Storage（对象）
                          │
                          └── Derived Engine（逻辑库，由 Runtime 调度执行）
```

## 2. 组件清单（Runtime 职责）

| 组件 | 职责 | 现状 / 来源 |
|---|---|---|
| Scheduler | 计算到期任务（cron / 手动），**只入队、不执行、不等待** | doc-2 §6.1；TASK-3.6 |
| Dispatcher | 依赖门控（Dependency Manager）+ 优先级 / 槽位 + 背压（有界队列），决定「能不能跑」 | 本文档（新增） |
| WorkerPool | 执行任务（线程 / 进程池）；不含调度与依赖判断 | 本文档（新增） |
| Dependency Manager | 依赖图解析与条件判定（`meta.job_dependencies`），供 Dispatcher 门控 | 本文档（新增，v1 预留） |
| Job Manager | 任务定义 / 状态机 / 幂等键 / 重试 / 补数 / 水位（元数据面） | 本文档（新增） |
| Sync Engine | 采集 / 回填 / 导入：调用 FinDataHub，staging+COPY 落 Canonical | TASK-3.6 / 3.3.2 |
| Derived Engine Executor | 执行派生（none / latest 物化、代次切换、重述重算） | TASK-3.12 |
| Read Model Builder | 构建/刷新 `mart.*`（视图/投影表），投影表原子 swap | TASK-3.12 / 3.10 |
| Quality Runner | 质量规则执行与结果记录（失败仅告警，不阻断读模型） | TASK-3.5（挂载） |
| Cache Manager | L2 缓存代际与按域失效（非权威、fail-open） | TASK-3.9 |
| Metadata Registry | `meta.*` 元数据读写（见 §5） | doc-13 §1 |
| Observability | 结构化日志、`job_runs` 查询、**按角色健康检查**、指标（可选 Prometheus） | 本文档 |
| Admin API | 控制面只读/操作面（供 WebUI）：任务状态、触发、水位、质量结果 | TASK-3.8 |

## 3. 进程与部署模型

### 3.1 单镜像多入口，职责分进程

- 一个应用镜像，多个入口点：`runtime`（常驻，本文档主体）、`rest`（只读 API）、`admin`（WebUI 后端，可选）；
- **Runtime 与 REST / admin 分进程、同镜像**（compose 独立 service）：共享代码与依赖注入，但进程与权限分离（见 §9）；
- **凭证经环境变量 / secret 注入，不落镜像**；Export 产物落**本地卷**（部署时挂载；对象存储后续评估）。

### 3.2 逻辑分层：Scheduler / Dispatcher / WorkerPool（同进程，职责先拆）

即使 v1 单进程运行，内部按角色分层，**层间以显式接口 / 内部有界队列通信，无共享可变状态**（状态在 `meta.*`）：

```
Scheduler ──(到期事件)──► Dispatcher ──(可执行任务)──► WorkerPool
                              ▲
                    Dependency Manager（依赖门控）
```

- **Scheduler**：只做时间计算与入队，**永不等待执行**——同步任务再多、再慢，也不阻塞调度节拍；
- **Dispatcher**：依赖门控、优先级与资源槽位、背压（有界队列；满时按优先级排队或拒绝低优先任务）；
- **WorkerPool**：只执行任务；池满只影响队列深度，不影响 Scheduler；
- **隔离原则**：任一角色卡死不得阻塞其他角色；各角色独立心跳与健康检查（scheduler 最近 tick、dispatcher 队列积压、worker 池占用 / 最近任务完成）。

**未来拆进程 = 换入口，不重构代码**：

```bash
runtime             # 全角色（v1 默认，单机）
runtime-scheduler   # 仅 Scheduler + Dispatcher
runtime-worker      # 仅 WorkerPool
```

拆分后的协调仍只依赖 PostgreSQL（`meta.job_runs` 队列 + advisory lock；可选 LISTEN/NOTIFY 降低轮询延迟），不引入中间件。

### 3.3 单副本起步，多副本防重

- v1 单机 Compose（doc-13 §8）：单副本直接运行；
- 多副本扩展时，防重不靠选举，靠 **PG advisory lock**（按任务作用域）；
- 不引入额外中间件：调度与队列复用 PostgreSQL；Redis 仅作可选 L2 缓存（缺失时 fail-open）。

### 3.4 启动与停机

启动顺序：

1. 加载配置（DSN / 字典路径 / 时区 / 日历）；
2. 校验 schema 版本与代码期望一致（迁移版本对比；不一致 fail fast，不自动改库）；
3. 加载数据字典并执行 CI 校验（失败则拒绝启动）；**字典不做热加载：变更后需重启 Runtime**；
4. 注册任务定义与依赖关系（声明式，镜像到 `meta.job_defs` / `meta.job_dependencies`），恢复上次状态（watermark / 失败重试）；
5. 按角色启动 Scheduler / Dispatcher / WorkerPool，暴露健康检查。

停机：停止接受新任务 → 等待运行中任务到超时 → 超时任务标记 `interrupted`（下次按幂等键重跑）。

## 4. 任务模型（核心）

### 4.1 任务定义（声明式）

任务不是散落的 cron，而是「任务定义 + 调度规则」：

- `job_id`：任务定义标识（在 `kind` 内的唯一名，与输入绑定）；
- `kind`：`sync` / `backfill` / `import` / `derive` / `build_rm` / `quality` / `cache_invalidate` / `export`；
- `dataset`：目标数据集（canonical 表标识）；
- `scope`：分区范围 / 窗口 / 参数；
- `schedule`：cron 或手动触发；
- `retry`：最大重试与退避策略；
- `owner` / `priority`。

任务定义来源：首期以代码注册（`@task` 装饰器或配置模块）镜像到 `meta.job_defs`；**参数与口径一律引用字典**，不在任务中硬编码字段。

### 4.2 幂等键（按 kind 的版本维度）

```
job_key = hash(kind, job_id, scope, window, version_dimension?)
```

- 重试、补数、重复触发共用同一幂等键语义；
- `version_dimension` **只在语义会变的两类任务上启用**：

| kind | 版本维度 | 原因 |
|---|---|---|
| `derive` | 算法身份 `id@vN`（如 `ma20@v1` → `ma20@v2`） | 算法 / 窗口逻辑变化 → 必须重算；版本由算法注册表（`meta.algorithm_registry`）承担，属**算法语义变更，不是字典变更** |
| `build_rm` | 读模型 `semantic_version`（`mart.<name>_v<N>`） | 读模型口径 / 契约变化 → 新版本必须重建 |
| 其余（`sync` / `backfill` / `import` / `quality` / `cache_invalidate` / `export`） | 无 | 数据 append-only + 物理键 `ON CONFLICT DO NOTHING` 已保证幂等，版本维度是多余概念 |

- **明确：幂等键不引入字典通用版本概念**；语义契约版本只在这两处生效，且来源明确（算法注册表 / 读模型版本）；
- PIT 重述（重算）通过上述版本维度表达，不覆盖历史。

### 4.3 依赖与触发条件（Dependency Gate）

数据集之间天然形成依赖链（示例）：

```
daily_bar → adj_factor → qfq_price（derived）→ ma20（derived）→ build_rm
```

- 依赖关系登记于 **`meta.job_dependencies`**：`parent_job` / `child_job` / `condition`；
- `condition` 首期仅 `on_success`（保留 `on_complete` / `always` 枚举位）；
- v1 语义 = **门控**：child 到期后需 parent 在对应窗口 / 版本成功，方可进入 Dispatcher 队列；
- 窗口对齐规则首期固定为「child 窗口 = parent 窗口」（跨窗口聚合依赖后续演进）；
- 依赖以 **job 粒度**登记（job 与 dataset/kind 绑定，见 4.1）。

**明确不上 DAG 编排引擎**：不做分支 / 汇合、动态展开、回填传播、失败跳过策略编排；依赖仅作门控与顺序放开。后续若需要工作流语义，再在 `meta.job_dependencies` 上扩展（Airflow / Dagster 仍排除，doc-13 §10）。

### 4.4 状态机

```
queued → running → succeeded
             │  ├── failed → retrying（≤ max_attempts）→ queued
             │  └── failed → dead（超限；需人工处置）
             └── cancelled / interrupted
```

APScheduler 采用 **PostgreSQL job store** 持久化调度注册（重启不丢触发计划）；**任务运行状态、重试与依赖放行的权威在 `meta.job_runs`**（不可只存内存）。

### 4.5 并发与锁

- 锁粒度：`pg_advisory_xact_lock(hash(dataset, partition_scope))`；随事务释放，不跨任务长期持有；
- 同一数据集同一窗口的并发任务互斥；不同数据集 / 窗口并行受 WorkerPool 上限约束；
- 与 v0 一致：源端限流 / 配额 / 请求合并复用 FinDataHub（TASK-3.6 AC#3）。

### 4.6 重试、补数与水位

- 重试：有限次 + 指数退避；`RateLimitTimeout` 等可重试异常分类处理；
- 水位（watermark）：每数据集（或分区）记录已覆盖窗口的最大事件 / 知识时间；
- 补数：按缺口生成 `backfill` 任务，与常规任务同幂等规则；历史不回写、按版本追加；
- 超限 `dead` 任务提供人工重放入口（Admin API），仍走同一幂等键。

## 5. 元数据（meta.*）

| 表 | 用途 | 写入方 |
|---|---|---|
| `meta.dataset_registry` | 字典运行时镜像（版本、字段摘要、SLA / 覆盖率） | Runtime 启动 / 刷新 |
| `meta.job_defs` | 任务定义镜像（kind / dataset / schedule / retry / priority） | Runtime 启动 / 刷新 |
| `meta.job_dependencies` | **任务依赖与触发条件（parent_job / child_job / condition）** | Runtime 启动 / 刷新（代码注册镜像） |
| `meta.job_runs` | 运行记录与状态机（含 attempt / rows / error / request_id） | Job Manager |
| `meta.watermarks` | 各数据集 / 分区水位 | Job Manager |
| `meta.quality_results` | 质量检查结果 | Quality Runner |
| `meta.data_generation` | 投影表 / 缓存代次 | Read Model Builder / Cache Manager |
| `meta.algorithm_registry` | 派生算法登记（doc-10 §3.5） | Derived Engine（代码 @register 生成，CI 与字典一致） |
| `meta.algorithm_events` | 算法升级 / 重述台账 | Derived Engine Executor |

约束：`meta.*` 是**可重建的运行时镜像与状态**，不是数据语义来源；清空后可由字典 + 代码重建（历史 job 运行记录除外）。

## 6. 数据流与事务边界

单任务执行（写侧）：

1. 取锁（作用域级）→ `job_runs` 标记 `running`；
2. 读水位与幂等键，确定窗口；
3. 经 FinDataHub 取数（限流 / 缓存 / 请求合并）；
4. staging（COPY）→ 幂等 merge（`ON CONFLICT`）→ PIT append-only 落 Canonical；
5. 提交后：更新 `job_runs` / `watermarks`；
6. **依赖放行**：Dependency Manager 依据 `meta.job_dependencies` 将 condition 满足的下游任务交 Dispatcher 入队（示例链：daily_bar → adj_factor → qfq → ma20 → build_rm）；
7. 同批副作用：读模型刷新（视图无需动作；投影表原子 swap + `data_generation` 递增）、缓存代际失效、质量检查任务入队；**质量失败仅告警，不阻断读模型构建**。

失败：事务回滚 + `job_runs` 记录错误（作业异常不污染数据）；重试按幂等键安全重放；父任务未成功时下游不放开（`condition=on_success`）。

## 7. 缓存治理

- Cache Manager 维护 L2（Redis，可选）代际与按域失效：提交后 `data_generation` 递增，旧代际读结果自然失效；
- 权威状态永远在 PostgreSQL；缓存不可用时直读权威（fail-open）；
- 键必须 PIT 安全（as-of / 版本 / 口径维度，doc-10 §3.4）。

## 8. 可观测

- 结构化日志（`request_id` 贯穿 job → 上游调用 → 写入）；
- 健康检查：liveness（进程活）、readiness（DB 可达 + 字典校验通过 + 迁移版本一致）；**按角色**：scheduler 最近 tick、dispatcher 队列积压 / 门控等待、worker 池占用与最近完成；
- 指标（可选）：job 成功率 / 耗时 / 积压、数据新鲜度（SLA）、行数异常；
- WebUI（TASK-3.8）经 Admin API 读取上述状态。

## 9. 安全与权限

- 写入端按 schema 最小授权（ingestion→raw/Canonical、derived→派生、import→raw、quality→meta）；
- 消费只读角色只可读 `mart`（TASK-3.3.3）；
- Admin API 独立鉴权与网络暴露面（doc-15 暂不制作：首期仅内网 / 本地）；
- REST / MCP / SDK 无写入权限，不得借 Runtime 间接写入。

## 10. 明确不做（v1）

- 分布式调度 / K8s / 多副本选举；
- 引入 Airflow / Dagster 等编排框架（APScheduler + PG 足够单机）；
- **通用 DAG 编排引擎**：不做分支 / 汇合、动态展开、回填传播（依赖仅作门控，见 4.3）；
- 多版本派生数据副本（doc-10 §3.5）；
- 实时流式摄取。

## 11. 评审决策与剩余待定

### 已决（2026-09-14 评审）

| 项 | 决策 |
|---|---|
| APScheduler job store | **保留**（PostgreSQL job store 持久化调度注册；运行状态权威仍在 `meta.job_runs`） |
| Runtime 与 REST | **分进程、同镜像**（compose 独立 service） |
| 字典热加载 | **不做**（变更后重启 Runtime） |
| Export 产物 | **本地卷**（对象存储后续评估） |
| Quality 失败 | **不阻断**读模型构建（仅告警） |
| 幂等键版本维度 | **仅两处**：`derive`→`algorithm_id`（算法语义版本，非字典变更）；`build_rm`→读模型 `semantic_version`；其余 kind 无版本维度 |

### 剩余待定（实施时复评）

1. **进程拆分时点**：v1 单进程全角色；建议出现首个长任务阻塞风险时切 `runtime-scheduler` / `runtime-worker`；
2. **依赖粒度**：建议 job 级（首期），dataset 级后续评估；`on_complete` / `always` 的启用时点。

## 12. 任务落位

| 事项 | 任务 |
|---|---|
| Runtime 骨架（角色分层入口 / Job + 依赖框架 / 健康检查 / 迁移校验） | TASK-3.18（骨架已落地；调度 / 派生执行随 3.6 / 3.12 挂载） |
| 调度与增量同步 | TASK-3.6（依赖 Runtime 骨架） |
| 导入通道 | TASK-3.3.2 |
| 质量执行 | TASK-3.5 |
| 派生执行与代次 | TASK-3.12 |
| Redis L2 | TASK-3.9 |
| WebUI / Admin API | TASK-3.8 |
| 镜像与编排 | TASK-3.4（部署 Runtime 与消费入口） |

