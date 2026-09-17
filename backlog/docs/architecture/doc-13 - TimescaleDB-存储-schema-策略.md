---
id: doc-13
title: TimescaleDB 存储 schema 策略
type: specification
created_date: '2026-09-13 12:40'
updated_date: '2026-09-17 12:19'
---
# TimescaleDB 存储 schema 策略

> 状态：**已冻结**（2026-09-13，第 3 稿） | 关联：doc-10 §1.2/§3.2/§4.2、doc-11（字典 `storage`）、doc-2 §6.13、TASK-3.1（AC#8）/ TASK-3.3
> 第 3 稿变更：新增 **§3.4 压缩与 PIT**（压缩不得改变查询语义；三模式一致性 CI 门禁；压缩参数登记字典 `storage.compression`）。
> 第 2 稿变更（采纳评审）：① `partition_by` → **`partition_strategy`（event_time | knowledge_time）**，并按 `pit_class` 默认推导（market→event_time、versioned→knowledge_time）；② **Canonical 不存 `is_latest`**（读侧派生，Read Model 计算/物化）；③ **默认不建外键**（高频事实表）；④ Read Model 三种实现显式化（View 默认 / Projection Table / 首期不用 Materialized View）。

## 1. 组织方式：schema = 层 × 域

| Schema | 内容 | 示例 |
|---|---|---|
| `raw` | 源端原始落地（append-only，审计/重放） | `raw.tushare_daily` |
| `<domain>` | Canonical 层（与 DataPanel 同名） | `cn_equity.daily_bar`、`cn_fund.nav`、`macro_cn.rate` |
| `mart` | Read Model | `mart.equity_daily_bar_v1` |
| `meta` | 控制面元数据 | `meta.dataset_registry`、`meta.job_runs`、`meta.watermarks`、`meta.quality_results`、`meta.data_generation`、`meta.algorithm_registry`、`meta.algorithm_events`（冻结稿修订 2026-09-13） |
| `ref` | 参照数据 | `ref.trade_calendar` |

### 1.1 Read Model 实现（三种，字典 `storage.read_model_impl` 声明）

| 实现 | 说明 | 使用场景 |
|---|---|---|
| `view`（**默认**） | 普通视图，零存储；as-of / `is_latest` 由查询条件派生 | 首期默认 |
| `projection_table` | 物理投影表：物化 `is_latest`、代次；**重建 = 新代次表 + 原子 rename/swap**，对应 `X-Data-Generation` | 单 view 性能不足时 |
| `materialized_view` | **首期不引入**：与 Timescale 连续聚合、PIT as-of、代次管理互相干扰（refresh 语义与 append-only/重述冲突） | 明确排除（未来评估） |

### 1.2 派生控制面表（修订 0004，TASK-3.12）

| 表 | 主键 | 说明 |
|---|---|---|
| `meta.algorithm_registry` | `algorithm_id` | 算法登记：`version / owner / implementation / dataset / output / inputs(JSON) / description / status(active\|deprecated) / effective_from`；由代码 `@register` + 字典生成，只 upsert 不删除 |
| `meta.algorithm_events` | `event_id`（唯一 `(algorithm_id, effective_from)`） | 算法升级 / 重述台账：`reason / created_at`（doc-10 §3.5，WebUI 可见） |
| `meta.data_generation` | `read_model` | 读模型 / 派生投影的构建代次：`generation` 格式 `YYYYMMDDTHHMMSSZ`（doc-12 `X-Data-Generation`；ETag/缓存键共用），`updated_at` |

物化投影（`materialize=latest`）命名 `mart.derived_<表>_<output>`：影子表 `__next` 重建后
事务内 `DROP + RENAME` 原子换名；表内附 `algorithm_id / as_of / computed_at / data_generation` 审计列；
**只保留一份**（可重建缓存，不落多版本）。

## 2. 表形态分类与存储策略

| 数据集类型 | 示例 | 存储形态 | 主键 | 分区策略 |
|---|---|---|---|---|
| 时序事实（`market`） | daily_bar、adj_factor、nav、index_weight | hypertable | `business_key + knowledge_time + version` 唯一 | **event_time** |
| 版本化时序（`versioned`） | financials、market_events | hypertable | 同上（append-only） | **knowledge_time** |
| 快照（`snapshot`） | index_weight（月度） | hypertable | `business_key + trade_date` | event_time |
| 属性区间（`scd2`） | ref.entity（SCD2）、index_member | 普通表 + 区间列 | `business_key + valid_from` | none |
| 主数据 | ref.entity、ref.entity_code_history | 普通表 | `entity_id` / `(entity_id, code)` | none |

## 3. Hypertable 设计

### 3.1 分区策略（显式字段，不混合语义）

- 字典字段：`storage.partition_strategy: event_time | knowledge_time | none`；
- **默认推导规则**（允许覆盖，覆盖需注释理由并由 CI 校验）：

| pit_class | 默认策略 | 原因 |
|---|---|---|
| `market` | `event_time` | 业务主查询按事件时间区间扫描；`knowledge_time` 用 BRIN 索引辅助 |
| `versioned` | `knowledge_time` | as-of 瓶颈在知识时间过滤：**按 knowledge_time 分区可裁剪 chunk**（例：2024 年报 knowledge=2025-04，`as_of=2025-03` 只需扫 2025 早期 chunk，而非全部 2024 chunk） |
| `snapshot` | `event_time` | 按快照日区间查询 |
| `scd2` | `none` | 普通表 + 区间索引 |

### 3.2 分区粒度（`storage.partition_interval`）

| 频率 | 建议 | 说明 |
|---|---|---|
| 日频（全市场） | 1 个月 | 单 chunk 目标 10M 行内 |
| 月频（宏观/权重） | 1 年 | chunk 过小增加开销 |
| 事件/低频 | 6 个月 | 视行数调整 |

### 3.3 空间分区

单机不启用 space partitioning；保留 hash（按 `entity_id`）选项（需字典显式声明）。

### 3.4 压缩与 PIT（Timescale 特有约束）

**原则**：压缩仅是物理存储优化，**不得改变任何查询语义**；`latest / as_of / history` 三种模式在压缩前后必须返回**逐行一致**的结果。

约束：

1. **仅压缩已封口 chunk**：`compress_after ≥ 分区边界 + 写入静默期`，避开重述/回填窗口；
2. 压缩 chunk **不支持 UPDATE/DELETE**（与 append-only 一致）：重述 = 插入新版本行；确需原地修正时走"解压 → 修正 → 重压"批处理并记录审计；
3. `segmentby / orderby` 只影响压缩效率与解压粒度，**禁止任何查询语义依赖它们**；排序/游标分页必须显式 `ORDER BY`，不得依赖物理顺序；
4. 压缩会改变执行计划与统计信息（chunk fast path、BRIN 交互），验收以**结果比对**为准，不以执行计划为准；
5. `is_latest` 为读侧派生（§4）：压缩后 view 窗口函数与 projection 物化结果必须一致；
6. 压缩参数变更（`segment_by / order_by / compress_after`）视为 schema 变更：走迁移 + CI 复验；
7. **压缩键必须覆盖物理键**：`segment_by ∪ order_by ⊇ physical_key`（TimescaleDB 唯一性要求；否则压缩块内唯一索引不被强制执行），CI 校验字典压缩配置；
8. **枚举列统一 `text`**（长度约束由字典 CI 承担，不用 `varchar(n)`）：避免扩展的类型建议告警；`ref` 与 canonical 表同源（字典条目优先）。

**CI 门禁（强制）**：

| 项 | 要求 |
|---|---|
| 样本 | 每个 `pit_class` 至少一个数据集 + 固定 `as_of` 集合 |
| 方法 | 压缩前/后执行同一查询（三模式全覆盖），结果规范化（显式排序 + 列投影 + NULL 归一）后哈希比对 |
| 边界 | chunk 边界日、`knowledge_time` 跨 chunk、含重述版本、含 NULL 行 |
| 失败处置 | **阻断发布**；压缩参数变更必须重跑 |

字典登记：`storage.compression: {after, segment_by, order_by}`（doc-11 冻结稿同步修订）。

## 4. 索引与约束

1. **唯一索引（物理键）**：`(business_key…, knowledge_time, version)`——append-only 幂等写入冲突判据；
2. **业务查询索引**：`(business_key, event_time)`；
3. **as-of 辅助**：`event_time` 分区表上对 `knowledge_time` 建 BRIN（append-only 单调）；`knowledge_time` 分区表按需对 `event_time` 建 btree；
4. **Canonical 不存 `is_latest`**：append-only 只存 `version / knowledge_time`；`is_latest` 是**读侧派生状态**——
   - Read Model = `view`：查询用窗口函数派生；
   - Read Model = `projection_table`：物化 `is_latest` 并建 `(business_key) WHERE is_latest` 部分索引；
   - 理由：派生列在重述/回填/补数下极易漏更新（文档§2 的 `version_mode` 已覆盖三种读语义，无需存储事实重复表达）；
5. **默认不建外键**：`entity_id` 不做 DB 级 FK（高频事实表 + Timescale 最佳实践，避免批量回填性能退化）；完整性由实体注册表（Entity Registry）与 Load Pipeline 校验；仅低基数维度表可酌情建 FK。

## 5. 压缩与保留（TimescaleDB 原生压缩）

| 层/类型 | 压缩策略 | 保留 |
|---|---|---|
| Raw | 封口 chunk 后压缩（`segmentby=source/entity_id`、`orderby=时间`） | 默认全量（审计） |
| Canonical（行情/因子） | 7 天后压缩（`segmentby=entity_id`、`orderby=event_time`） | 全量 |
| Canonical（财务/事件） | 30 天后压缩 | 全量（版本保留） |
| Read Model | 由 Canonical 派生，不单独压缩 | 随源 |

压缩参数（`compress_segmentby/compress_orderby/compress_after`）登记字典 `storage.compression`，由迁移生成；解压仅用于回填/重算批次；语义一致性门禁见 §3.4。

## 6. 连续聚合（Continuous Aggregates）使用边界

- **仅运维/统计类**：覆盖率日统计、新鲜度 lag、质量扫描、成本统计；
- **不用于业务派生**（归 TASK-3.12 派生引擎 + `algorithm_id` 版本管理）；
- 与 Read Model 的 `materialized_view` 明确区分：首期均不用于业务数据。

## 7. 迁移与幂等（Alembic + SQLAlchemy Core）

1. **Schema First**：DDL 从数据字典生成（doc-11 §7），CI 校验"字典 ↔ DDL"一致；
2. Alembic 迁移中通过 `create_hypertable`、压缩声明落地；
3. **幂等写入**：`INSERT … ON CONFLICT (physical_key) DO NOTHING`；
4. **数据代次（针对 `projection_table`）**：新代次表构建 → 原子 `rename/swap` → `meta.data_generation` 记录（对应 REST `X-Data-Generation`）；`view` 实现无代次切换问题；
5. 读写 DSN 分离（写入端最小授权 / 读端只读角色）；
6. **角色与授权（落地口径）**：只读 / 可写两分——`fdp_ro`（NOLOGIN）授予 `mart` + `ref` + 各数据域 canonical 的 `USAGE/SELECT` 与 `mart` 函数 `EXECUTE`，**不含 `raw` / `meta`**；写权限不授（数据库强制拒绝）；`DEFAULT PRIVILEGES` 覆盖新表；授权入口 `python -m fin_data_platform.storage.grants`（部署一次性服务，幂等）。
   **演进方向（文档化）**：按域拆分 `fdp_ro_<domain>`（基础 + 单域 canonical）实现逐域最小授权；个人平台现阶段保持两分，避免角色矩阵复杂化。

## 8. 备份、容量与部署

- 备份：`pg_dump` + 可选 WAL 归档；compose 卷持久化；
- 容量估算（示例）：日频全 A ≈ 150 万行/表/年（6000 标的 × 250 日）；压缩比典型 5~10×；
- 只读副本与读写 DSN 分离预留（TASK-3.10）。

## 9. 与字典的映射

| 字典键 | 存储落地 |
|---|---|
| `storage.canonical_table` | `<domain>.<dataset>` |
| `storage.read_model` | `mart.<name>_v<semantic_version>` |
| `storage.read_model_impl` | `view`（默认）/ `projection_table` |
| `storage.partition_strategy` | hypertable 分区列（默认按 `pit_class` 推导） |
| `storage.partition_interval` | chunk interval |
| `storage.retention` | 保留策略 |
| `storage.compression` | `{after, segment_by, order_by}`（语义一致性 CI 见 §3.4） |
| `physical_key` | 唯一索引 |
| `business_key` | 业务索引；`is_latest` 由读侧派生（不落 Canonical） |

## 10. 决策记录（2026-09-13）

| # | 决策 | 结论 |
|---|---|---|
| 1 | schema 组织 | 域 schema（`cn_equity.daily_bar` 与 DataPanel 同名）+ `mart/meta/raw/ref` |
| 2 | 分区策略 | `partition_strategy` 显式字段；默认 `market→event_time`、`versioned→knowledge_time`、`snapshot→event_time` |
| 3 | `is_latest` | **Canonical 不存**；Read Model 派生（view 窗口函数 / projection 物化） |
| 4 | 外键 | **默认不建**；实体注册表 + Load Pipeline 校验 |
| 5 | Read Model 实现 | `view` 默认；性能不足 → `projection_table`；**首期不用 materialized_view** |
| 6 | Raw 保留 | 默认全量（审计） |
| 7 | 连续聚合 | 仅运维统计 |
| 8 | 代次切换 | projection_table 新代次表 + rename/swap |
| 9 | 压缩与 PIT | 压缩不改写语义；三模式压缩前后逐行一致（CI 门禁，§3.4） |
