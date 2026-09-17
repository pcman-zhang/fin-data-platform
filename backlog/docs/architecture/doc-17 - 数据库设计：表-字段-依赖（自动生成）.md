---
id: doc-17
title: 数据库设计：表 / 字段 / 依赖（自动生成）
type: specification
created_date: '2026-09-13 14:07'
updated_date: '2026-09-17 12:28'
---
# 数据库设计：表 / 字段 / 依赖（自动生成）

> 由数据字典与 实体注册表 schema 生成（Schema First）；请勿手改，变更走字典。

## 1. 表清单与作用

| 表 | 作用 | 物理键 | 分区策略 |
|---|---|---|---|
| `cn_equity.adj_factor` | 复权因子（累计后复权口径；事件步进，按知识时间版本化） | entity_id, trade_date, knowledge_time, version | event_time |
| `cn_equity.daily_bar` | A 股日线行情（不复权原始价；复权价按 raw + factor、as-of 计算） | entity_id, trade_date, knowledge_time, version | event_time |
| `cn_equity.financials_balance_sheet` | 资产负债表（核心列；append-only 版本，按公告日知识时间；以 issuer_id 为键） | issuer_id, end_date, report_type, knowledge_time, version | knowledge_time |
| `cn_equity.index_member` | 申万行业成分（三级；区间型 PIT，含已剔除记录） | entity_id, l3_code, in_date | none |
| `cn_equity.index_weight` | 指数成分与权重（月度快照；快照型 PIT，as-of 取最近一期） | index_entity_id, trade_date, con_entity_id, knowledge_time, version | event_time |
| `cn_equity.listing_lifecycle` | 交易状态（上市/暂停/退市）；PIT Universe 权威来源，替代注册表交易状态 | entity_id, start_date, knowledge_time, version | none |
| `cn_equity.market_events_namechange` | 名称变更历史（生效闭区间 + 公告日；用于 as-of 属性还原） | entity_id, start_date, knowledge_time, version | none |
| `cn_fund.nav` | 场外基金净值（单位净值/累计净值；日频） | entity_id, date, knowledge_time, version | event_time |
| `meta.algorithm_events` | 算法升级 / 重述台账（algorithm_id / effective_from / reason） | event_id | — |
| `meta.algorithm_registry` | 派生算法登记（历史 id 永久保留） | algorithm_id | — |
| `meta.data_generation` | 读模型 / 派生投影构建代次（doc-12 X-Data-Generation） | read_model | — |
| `meta.job_defs` | Runtime 任务定义镜像（声明式注册；doc-20） | job_id | — |
| `meta.job_dependencies` | 任务依赖与触发条件（parent_job / child_job / condition） | parent_job, child_job | — |
| `meta.job_runs` | 任务运行记录与状态机（Runtime 状态权威） | run_id | — |
| `meta.watermarks` | 数据集 / 分区水位 | dataset, scope | — |
| `ref.entity` | 实体注册表（实体身份 + 分类面 + PIT 属性；SCD2；issuer/listing/series/basket） | entity_id, valid_from, knowledge_time, version | none |
| `ref.entity_code_history` | canonical 代码履历（代码变更/复用 → 旧码仍可解析；替代多源别名表） | entity_id, code, valid_from, knowledge_time, version | none |
| `ref.entity_external_id` | 实体外部标识（isin/figi/cusip/sedol/lei/uscc；不含 ticker） | entity_id, id_type, id_value, valid_from, knowledge_time, version | none |
| `ref.entity_relation` | 实体关系（单向存储；双向查询由 relation_type_dict.inverse_relation 驱动） | entity_id, related_id, relation_type, valid_from, knowledge_time, version | none |
| `ref.relation_type_dict` | 关系词表（新增关系词必须先登记；双向查询由 inverse_relation 驱动） | relation_type, valid_from, knowledge_time, version | none |

## 2. 字段与类型

### `cn_equity.adj_factor`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 平台标的 ID |
| `trade_date` | `DATE` | 否 |  | event_time | 交易日（事件步进生效日） |
| `adj_factor` | `NUMERIC(20, 6)` | 否 |  | none | 累计后复权因子（归一化后跨源一致；锚点由来源定义） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `ingest_time` | `DATETIME` | 否 |  | ingest_time | 物理入库时间（审计） |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号 |
| `provider` | `TEXT` | 否 |  | none | 因子来源 |

### `cn_equity.daily_bar`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 平台实体 ID（实体注册表主键） |
| `trade_date` | `DATE` | 否 |  | event_time | 交易日 |
| `open` | `DOUBLE` | 是 | 元 | none | 开盘价（不复权） |
| `high` | `DOUBLE` | 是 | 元 | none | 最高价（不复权） |
| `low` | `DOUBLE` | 是 | 元 | none | 最低价（不复权） |
| `close` | `DOUBLE` | 是 | 元 | none | 收盘价（不复权） |
| `volume` | `DOUBLE` | 是 | 股 | none | 成交量 |
| `amount` | `NUMERIC(24, 4)` | 是 | 元 | none | 成交额（精确金额） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `publish_time` | `DATETIME` | 是 |  | publish_time | 行情发布/可得时间（若源提供） |
| `ingest_time` | `DATETIME` | 否 |  | ingest_time | 物理入库时间（审计） |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号（append-only） |
| `provider` | `TEXT` | 否 |  | none | 该行来源（provider 维度） |

### `cn_equity.financials_balance_sheet`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `issuer_id` | `BIGINT` | 否 |  | none | 发行主体 ID（ref.entity，entity_type=issuer） |
| `ann_date` | `DATE` | 否 |  | publish_time | 公告日（知识时间来源） |
| `end_date` | `DATE` | 否 |  | event_time | 报告期末（事件时间） |
| `report_type` | `TEXT` | 否 |  | none | 报表类型（1 合并报表等） |
| `total_assets` | `NUMERIC(24, 4)` | 是 | 元 | none | 资产总计 |
| `total_liab` | `NUMERIC(24, 4)` | 是 | 元 | none | 负债合计 |
| `total_cur_assets` | `NUMERIC(24, 4)` | 是 | 元 | none | 流动资产合计 |
| `total_cur_liab` | `NUMERIC(24, 4)` | 是 | 元 | none | 流动负债合计 |
| `money_cap` | `NUMERIC(24, 4)` | 是 | 元 | none | 货币资金 |
| `inventories` | `NUMERIC(24, 4)` | 是 | 元 | none | 存货 |
| `fix_assets` | `NUMERIC(24, 4)` | 是 | 元 | none | 固定资产 |
| `goodwill` | `NUMERIC(24, 4)` | 是 | 元 | none | 商誉 |
| `total_hldr_eqy_exc_min_int` | `NUMERIC(24, 4)` | 是 | 元 | none | 归母股东权益合计 |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `ingest_time` | `DATETIME` | 否 |  | ingest_time | 物理入库时间 |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号（重述产生新版本） |
| `provider` | `TEXT` | 否 |  | none | 数据来源 |

### `cn_equity.index_member`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 标的 ID |
| `l1_code` | `TEXT` | 否 |  | none | 申万一级行业代码（index_code） |
| `l1_name` | `TEXT` | 否 |  | none | 申万一级行业名称 |
| `l2_code` | `TEXT` | 否 |  | none | 申万二级行业代码 |
| `l2_name` | `TEXT` | 否 |  | none | 申万二级行业名称 |
| `l3_code` | `TEXT` | 否 |  | none | 申万三级行业代码 |
| `l3_name` | `TEXT` | 否 |  | none | 申万三级行业名称 |
| `in_date` | `DATE` | 否 |  | event_time | 纳入日期（区间起点，含当日） |
| `out_date` | `DATE` | 是 |  | none | 剔除日期（区间终点，含当日；NULL=至今） |
| `is_new` | `BOOLEAN` | 否 |  | none | 是否当前有效归属 |

### `cn_equity.index_weight`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `index_entity_id` | `BIGINT` | 否 |  | none | 指数标的 ID |
| `trade_date` | `DATE` | 否 |  | event_time | 快照日（月度） |
| `con_entity_id` | `BIGINT` | 否 |  | none | 成分标的 ID |
| `weight` | `NUMERIC(12, 6)` | 否 | 百分数 | none | 成分权重（百分数） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `ingest_time` | `DATETIME` | 否 |  | ingest_time | 物理入库时间 |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号 |
| `provider` | `TEXT` | 否 |  | none | 数据来源 |

### `cn_equity.listing_lifecycle`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 标的 ID |
| `status` | `TEXT` | 否 |  | none | 交易状态（listed 上市/suspended 暂停上市/delisted 退市） |
| `start_date` | `DATE` | 否 |  | event_time | 状态生效起始日（含当日） |
| `end_date` | `DATE` | 是 |  | none | 状态生效结束日（含当日；NULL=至今） |
| `reason` | `TEXT` | 是 |  | none | 状态变更原因 |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号 |
| `provider` | `TEXT` | 否 |  | none | 数据来源 |

### `cn_equity.market_events_namechange`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 标的 ID |
| `name` | `TEXT` | 否 |  | none | 证券简称（该区间内） |
| `start_date` | `DATE` | 否 |  | event_time | 名称生效起始日（含当日） |
| `end_date` | `DATE` | 是 |  | none | 名称生效结束日（含当日；NULL=至今） |
| `ann_date` | `DATE` | 是 |  | publish_time | 公告日（知识时间来源） |
| `change_reason` | `TEXT` | 是 |  | none | 变更原因 |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号 |
| `provider` | `TEXT` | 否 |  | none | 数据来源 |

### `cn_fund.nav`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 基金标的 ID |
| `date` | `DATE` | 否 |  | event_time | 净值日期 |
| `unit_nav` | `NUMERIC(16, 6)` | 否 | 元 | none | 单位净值 |
| `accum_nav` | `NUMERIC(16, 6)` | 是 | 元 | none | 累计净值 |
| `daily_return` | `DOUBLE` | 是 | 百分数 | none | 日增长率（百分数） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `ingest_time` | `DATETIME` | 否 |  | ingest_time | 物理入库时间 |
| `version` | `BIGINT` | 否 |  | none | 同业务键版本号 |
| `provider` | `TEXT` | 否 |  | none | 数据来源 |

### `meta.algorithm_events`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `event_id` | `INTEGER` | 否 |  |  |  |
| `algorithm_id` | `VARCHAR(64)` | 否 |  |  |  |
| `effective_from` | `DATE` | 否 |  |  |  |
| `reason` | `TEXT` | 否 |  |  |  |
| `created_at` | `DATETIME` | 否 |  |  |  |

### `meta.algorithm_registry`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `algorithm_id` | `VARCHAR(64)` | 否 |  |  |  |
| `version` | `INTEGER` | 否 |  |  |  |
| `owner` | `VARCHAR(64)` | 否 |  |  |  |
| `implementation` | `VARCHAR(255)` | 否 |  |  |  |
| `dataset` | `VARCHAR(64)` | 是 |  |  |  |
| `output` | `VARCHAR(64)` | 是 |  |  |  |
| `inputs` | `TEXT` | 是 |  |  |  |
| `description` | `TEXT` | 否 |  |  |  |
| `status` | `VARCHAR(16)` | 否 |  |  |  |
| `effective_from` | `DATE` | 是 |  |  |  |
| `updated_at` | `DATETIME` | 否 |  |  |  |

### `meta.data_generation`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `read_model` | `VARCHAR(128)` | 否 |  |  |  |
| `generation` | `VARCHAR(32)` | 否 |  |  |  |
| `updated_at` | `DATETIME` | 否 |  |  |  |

### `meta.job_defs`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `job_id` | `VARCHAR(64)` | 否 |  |  |  |
| `kind` | `VARCHAR(16)` | 否 |  |  |  |
| `dataset` | `VARCHAR(64)` | 否 |  |  |  |
| `schedule` | `VARCHAR(64)` | 是 |  |  |  |
| `priority` | `INTEGER` | 否 |  |  |  |
| `max_attempts` | `INTEGER` | 否 |  |  |  |
| `enabled` | `BOOLEAN` | 否 |  |  |  |
| `updated_at` | `DATETIME` | 否 |  |  |  |

### `meta.job_dependencies`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `parent_job` | `VARCHAR(64)` | 否 |  |  |  |
| `child_job` | `VARCHAR(64)` | 否 |  |  |  |
| `condition` | `VARCHAR(16)` | 否 |  |  |  |
| `updated_at` | `DATETIME` | 否 |  |  |  |

### `meta.job_runs`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `run_id` | `INTEGER` | 否 |  |  |  |
| `job_key` | `VARCHAR(64)` | 否 |  |  |  |
| `job_id` | `VARCHAR(64)` | 否 |  |  |  |
| `kind` | `VARCHAR(16)` | 否 |  |  |  |
| `dataset` | `VARCHAR(64)` | 否 |  |  |  |
| `scope` | `VARCHAR(64)` | 否 |  |  |  |
| `window_start` | `DATE` | 是 |  |  |  |
| `window_end` | `DATE` | 是 |  |  |  |
| `version_dimension` | `VARCHAR(64)` | 是 |  |  |  |
| `status` | `VARCHAR(16)` | 否 |  |  |  |
| `attempt` | `INTEGER` | 否 |  |  |  |
| `max_attempts` | `INTEGER` | 否 |  |  |  |
| `priority` | `INTEGER` | 否 |  |  |  |
| `scheduled_at` | `DATETIME` | 否 |  |  |  |
| `started_at` | `DATETIME` | 是 |  |  |  |
| `finished_at` | `DATETIME` | 是 |  |  |  |
| `rows_written` | `BIGINT` | 是 |  |  |  |
| `error` | `TEXT` | 是 |  |  |  |
| `request_id` | `VARCHAR(64)` | 是 |  |  |  |
| `worker` | `VARCHAR(64)` | 是 |  |  |  |
| `updated_at` | `DATETIME` | 否 |  |  |  |

### `meta.watermarks`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `dataset` | `VARCHAR(64)` | 否 |  |  |  |
| `scope` | `VARCHAR(64)` | 否 |  |  |  |
| `watermark_time` | `DATETIME` | 是 |  |  |  |
| `updated_at` | `DATETIME` | 否 |  |  |  |

### `ref.entity`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 实体稳定代理键 |
| `entity_type` | `TEXT` | 否 |  | none | 实体本体（粗分类） |
| `entity_class` | `TEXT` | 是 |  | none | 产品细分（entity_type 之下；缺省为未知） |
| `market` | `TEXT` | 是 |  | none | 市场面 |
| `code` | `TEXT` | 否 |  | none | canonical 代码（WindCode 风格；issuer 取统一社会信用代码或平台码） |
| `name` | `TEXT` | 否 |  | none | 时点名称（变更产生新 SCD2 行） |
| `currency` | `TEXT` | 是 |  | none | ISO 4217 货币 |
| `exchange` | `TEXT` | 是 |  | none | 交易所/市场 |
| `frequency` | `TEXT` | 是 |  | none | 序列频率（series 使用） |
| `unit` | `TEXT` | 是 |  | none | 单位（series 使用） |
| `algorithm_id` | `TEXT` | 是 |  | none | 组合/派生序列对应算法（basket） |
| `social_status` | `TEXT` | 是 |  | none | 社会实体状态（issuer 专用：存续/倒闭/重整） |
| `valid_from` | `DATE` | 否 |  | event_time | 属性区间起点（SCD2 闭区间） |
| `valid_to` | `DATE` | 是 |  | none | 属性区间终点（NULL=至今） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 同实体版本号（append-only） |

### `ref.entity_code_history`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 实体稳定代理键 |
| `code` | `TEXT` | 否 |  | none | canonical 代码（含历史代码） |
| `valid_from` | `DATE` | 否 |  | event_time | 代码生效起始 |
| `valid_to` | `DATE` | 是 |  | none | 代码失效日（NULL=至今） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 版本号 |

### `ref.entity_external_id`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 实体 ID |
| `id_type` | `TEXT` | 否 |  | none | 外部标识类型（不含 ticker） |
| `id_value` | `TEXT` | 否 |  | none | 外部标识值 |
| `valid_from` | `DATE` | 否 |  | event_time | 标识区间起点（SCD2 闭区间） |
| `valid_to` | `DATE` | 是 |  | none | 标识区间终点（NULL=至今） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 同标识版本号（append-only） |

### `ref.entity_relation`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `entity_id` | `BIGINT` | 否 |  | none | 关系主体实体 ID |
| `related_id` | `BIGINT` | 否 |  | none | 关系对象实体 ID |
| `relation_type` | `TEXT` | 否 |  | none | 关系词（必须登记于 ref.relation_type_dict） |
| `valid_from` | `DATE` | 否 |  | event_time | 关系区间起点（SCD2 闭区间） |
| `valid_to` | `DATE` | 是 |  | none | 关系区间终点（NULL=至今） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 同关系版本号（append-only） |

### `ref.relation_type_dict`

| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |
|---|---|---|---|---|---|
| `relation_type` | `TEXT` | 否 |  | none | 关系词 |
| `inverse_relation` | `TEXT` | 否 |  | none | 反向关系词（词表内成对登记） |
| `description` | `TEXT` | 否 |  | none | 语义说明（主体 → 对象） |
| `valid_from` | `DATE` | 否 |  | event_time | 生效起点（SCD2 闭区间） |
| `valid_to` | `DATE` | 是 |  | none | 生效终点（NULL=至今） |
| `knowledge_time` | `DATETIME` | 否 |  | knowledge_time | 该版本进入平台的时间 |
| `version` | `BIGINT` | 否 |  | none | 同词条版本号（append-only） |

## 3. 表依赖关系（逻辑，无物理外键；doc-13 §4）

> `entity_id`/`issuer_id` 为平台稳定代理键（BIGINT，代理键非源代码）；
> 按 doc-13 §4 **默认不建物理外键**（hypertable 压缩与批量回填约束、
> SCD2 主键为 `(entity_id, valid_from)` 无法被事实表单列引用）。
> 引用完整性由三层保障：① 写入管线校验；
> ② 质量规则（`reconcile against: ref.entity`）；
> ③ 读取时以 `entity_id` 关联 `ref.entity`（名称/类型/退市属性）。

- `cn_equity.adj_factor` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_equity.daily_bar` ← cn_equity.adj_factor（派生输入）；ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_equity.financials_balance_sheet` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_equity.index_member` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_equity.index_weight` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_equity.listing_lifecycle` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_equity.market_events_namechange` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `cn_fund.nav` ← ref.entity（entity_id/issuer_id 逻辑引用）
- `meta.algorithm_events` ← —（源数据）
- `meta.algorithm_registry` ← —（源数据）
- `meta.data_generation` ← —（源数据）
- `meta.job_defs` ← —（源数据）
- `meta.job_dependencies` ← —（源数据）
- `meta.job_runs` ← —（源数据）
- `meta.watermarks` ← —（源数据）
- `ref.entity` ← —（源数据）
- `ref.entity_code_history` ← ref.entity（entity_id/issuer_id 逻辑引用）；ref.entity（血缘）
- `ref.entity_external_id` ← ref.entity（entity_id/issuer_id 逻辑引用）；ref.entity（血缘）
- `ref.entity_relation` ← ref.entity（entity_id/issuer_id 逻辑引用）；ref.entity（血缘）
- `ref.relation_type_dict` ← —（源数据）
