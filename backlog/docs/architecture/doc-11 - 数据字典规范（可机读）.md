---
id: doc-11
title: 数据字典规范（可机读）
type: specification
created_date: '2026-09-13 12:16'
updated_date: '2026-09-17 17:58'
---
# 数据字典规范（可机读）

> 状态：**已冻结**（2026-09-13，第 4 稿） | 关联：doc-10（架构总纲 §6.1 Schema First）、TASK-3.1/3.2/3.12/3.14
> 冻结稿修订（2026-09-13，由 doc-13 评审引入）：`storage` 字段改为 `{canonical_table, read_model, read_model_impl, partition_strategy, partition_interval, retention, compression}`（compression 语义一致性 CI 见 doc-13 §3.4）；`is_latest` 不在 Canonical 落列（读侧派生）。
> 第 4 稿变更：派生指标改为**代码实现 + `algorithm_id` 登记**（无描述表达式）；新增算法注册/审计约定与三方一致性 CI。
> 第 3 稿变更：§10 四项决策落定——YAML；**每数据集一文件**（目录即域，避免单文件 5000+ 行无法 review）；expression 首期仅比较/逻辑/算术（窗口/聚合/join 归派生引擎）；mappings 不含单位换算（归适配器 spec）。
> 第 2 稿变更（采纳评审）：① `semantic_version` 为整数（仅主版本）；② decimal 必带 `precision/scale`；③ 拆 `business_key` / `physical_key`；④ 质量规则支持**跨字段表达式**；⑤ `source_mappings` 移出 field，改为 dataset 级 `mappings`；⑥ `coverage` 增加可计算维度；⑦ `lineage` 强制；⑧ **字典不登记公式**——派生仅登记 `inputs/output/owner`，公式归 TASK-3.12 派生引擎。

## 1. 原则

1. **Schema First**：Dictionary → Schema → SDK/API；禁止"代码先写、文档后补"；
2. **覆盖完整**：每个 DataPanel 必须登记字段/类型/单位/PIT 类别/约束/血缘/SLA/覆盖/存储/质量；
3. **Provider 无关**（doc-10 §6.2）：canonical 字段不得含供应商品牌/缩写；源差异经 `mappings` 登记，不进入公共 schema；
4. **语义版本**：`semantic_version`（整数，仅主版本）；纯增量不升，口径/单位/语义变化 +1；
5. **血缘强制**：所有 DataPanel 必须有 `lineage`（源数据用 `upstream: []` + `transform: raw` 显式声明，区分"无血缘"与"漏写"）；
6. **公式不入字典**：字典只登记派生依赖（`inputs/output/owner`）；公式文本、版本与重算策略由 TASK-3.12 派生引擎注册表管理，避免出现两套 DSL；
7. **机读优先**：可被程序解析、校验、生成；人读文档由字典渲染。

## 2. 文件形态与目录（已定）

- 格式：**YAML**（嵌套深——lineage/coverage/mappings；TOML 可读性不足）+ **Pydantic v2** 严格校验；元 schema 由模型导出（JSON Schema，CI 双向校验）；
- **每数据集一文件**（不做每域一文件：单域 50+ DataPanel 会到 5000+ 行，无法 code review）：
  一个 YAML 文件 = 一个 dataset 条目，文件名 = 数据集末段，目录 = 数据域：

```
platform/dictionary/
  _schema/dictionary.schema.json
  cn_equity/
    daily_bar.yaml          # cn_equity.daily_bar
    adj_factor.yaml         # cn_equity.adj_factor
    index_member.yaml       # cn_equity.index_member
    financials/
      balance_sheet.yaml    # cn_equity.financials.balance_sheet（子类建子目录，仍一数据集一文件）
  cn_fund/
    nav.yaml                # cn_fund.nav
  macro_cn/
    rate.yaml               # macro_cn.rate
```

- 命名：`dataset` = `{domain}.{dataset}`（小写蛇形）；文件路径必须与 dataset 一致（CI 校验）；字段小写蛇形、禁止源前缀。

## 3. 条目结构（meta-schema）

### 3.1 dataset 级

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `dataset` | str | ✅ | `{domain}.{dataset}`，全局唯一 |
| `semantic_version` | int | ✅ | 语义主版本（1、2…）；增量不升 |
| `domain` | enum | ✅ | doc-10 §3.1 的 DataDomain |
| `description` | str | ✅ | 口径、用途、注意事项 |
| `pit_class` | enum | ✅ | `market` / `versioned` / `scd2` / `snapshot` |
| `business_key` | [str] | ✅ | 业务主键（如 `[entity_id, trade_date]`） |
| `physical_key` | [str] | ✅ | 物理唯一键（append-only：`business_key + knowledge_time + version`） |
| `grain` | str | ✅ | 粒度描述 |
| `update_sla` | obj | ✅ | `{frequency, earliest_available, latest_available, tolerance}` |
| `sources` | [obj] | ✅ | `[{provider, endpoint, note?}]`（采集入口） |
| `coverage` | obj | ✅ | `{universe, universe_source, history_start, expected_dates}`（可计算覆盖率，见 §3.3） |
| `storage` | obj | ✅ | `{canonical_table, read_model, read_model_impl, partition_strategy, partition_interval, retention, compression}`（见 doc-13） |
| `quality` | [obj] | ✅ | 规则列表（§3.4，含跨字段表达式） |
| `lineage` | obj | ✅ | `{upstream: [{dataset, fields?}], transform}`（源数据为 `upstream: []` + `transform: raw`） |
| `derived` | [obj] | | 派生登记：`[{output, algorithm_id, implementation, owner, inputs, description}]`（**代码实现，无公式文本**，见 §4） |
| `mappings` | [obj] | ✅ | 源映射：`[{provider, endpoint, fields: {canonical: source}}]`（§3.5） |
| `adjust` | obj | | 复权口径声明（访问面执行）：`{modes, factor_dataset, factor_field, fields, default}`（§3.7；不登记 = 无复权） |
| `fields` | [obj] | ✅ | 字段列表（§3.2） |

### 3.2 field 级

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | canonical 字段名（provider 无关） |
| `type` | enum | ✅ | `int64 / float64 / decimal / string / bool / date / timestamp / timestamp_tz / enum` |
| `precision` / `scale` | int | decimal 必填 | 如 `precision: 20, scale: 6` → DDL `NUMERIC(20,6)`（price/nav/market_value 等必须精确） |
| `unit` | str? | | 元/股/%/点…；百分数需注明 |
| `nullable` | bool | ✅ | 是否可空 |
| `enum` | [str] | | type=enum 时必填 |
| `range` | obj | | `{min, max}` |
| `description` | str | ✅ | 含义与口径 |
| `pit_role` | enum | ✅ | `event_time / publish_time / knowledge_time / ingest_time / none` |

### 3.3 coverage（可计算）

```yaml
coverage:
  universe: A 股（含退市）
  universe_source: cn_equity.listing_lifecycle      # 期望实体集合来源
  history_start: 1990-12-19
  expected_dates:
    calendar: cn_equity.trade_calendar            # 期望日期集合来源
    frequency: daily
```

覆盖率（CI/质量平台自动计算）：`实际行数 / (期望实体数(as-of) × 期望日期数)`；避免覆盖率停留在描述文字。

### 3.4 quality 规则（含跨字段表达式）

| rule | 参数 | 示例 |
|---|---|---|
| `unique` | `keys` | `keys: [entity_id, trade_date]` |
| `not_null` | `fields` | |
| `range` | `field, min, max` | `volume >= 0` |
| `enum` | `field, values` | |
| `expression` | `expr, severity` | `expr: "high >= low and low <= close <= high"`；**首期仅比较/逻辑/算术 + 显式字段引用**；禁 window/aggregate/join（归派生引擎） |
| `reconcile` | `against` | 与 Raw/外部源对账（TASK-4.1） |
| `freshness` | `sla, tolerance` | |

`expression` 支持跨字段/跨列约束，字段必须存在于本数据集；表达式语言与派生引擎共用（TASK-3.12），字典只登记文本与严重级别。

### 3.5 mappings（dataset 级，Schema 与 Adapter 分离）

```yaml
mappings:
  - provider: tushare
    endpoint: daily
    fields:
      close: close
      volume: vol
      amount: amount
  - provider: baostock
    endpoint: query_history_k_data_plus
    fields:
      close: close
      volume: volume
```

- 每个 provider 一段映射；canonical 字段 → 源字段 **仅字段名**；
- **不含单位换算/枚举映射**：换算由**适配器 spec（v0）**负责；字典只描述最终 canonical 语义；
- field 级不再出现 `source_mappings`，避免单字段挂 100+ 源映射导致的膨胀；
- 与 v0 `fin_data_hub/specs/*.toml` 交叉校验：平台 `mappings` ⊇ 适配器映射。

### 3.7 adjust（复权口径声明；访问面执行）

```yaml
adjust:
  modes: [qfq, hfq]                  # 支持的口径（raw 恒可用，无需列出）
  factor_dataset: cn_equity.adj_factor
  factor_field: adj_factor
  fields: [open, high, low, close]   # 可复权字段（显式声明，读取层不推断）
  default: qfq                       # 读取缺省口径（none = 不调整）
```

规则（TASK-3.24 落地）：

1. **可复权数据集才登记**：不登记 = 无复权（指数 / 财务 / 宏观）；对它请求 `qfq/hfq`
   报 `unsupported_adjust`，不静默返回原值；
2. **组合单一实现**：`qfq = raw × f / f_anchor`（锚点 = `as_of` 可见因子中按事件时间最新）、
   `hfq = raw × f`；按需读取与读模型内联共用同一 SQL（访问面），派生算法不再自拼复权；
3. **CI 校验**：`modes` 非空且不重复、`default ∈ modes ∪ {none}`、`factor_dataset` 存在且
   非自身、`factor_field` 存在于因子数据集、`fields` 存在于本数据集、因子数据集业务键为
   本数据集业务键子集（按键 join）；
4. **派生输入引用**：`dataset.field[@mode]`（缺省取本数据集 `adjust.default`）；`@mode`
   须在本数据集已声明的 `modes` 内（CI 与运行时一致校验）。

## 4. 派生数据：代码实现 + 算法登记（无描述表达式）

派生指标一律以**代码实现**（附结构化注释、性能可控、可测试），字典登记算法元数据；**不存多版本派生结果**（存储成本），最多保留"最新一份"可重建投影。

```yaml
derived:
  - output: ma20
    algorithm_id: ma20_v1
    implementation: finplatform.derived.factors.ma20
    owner: derived-engine
    inputs:
      - cn_equity.daily_bar.close        # 输入经规范化读取层（含复权组合），非原始 OHLCV
    description: 20 日收盘价均线
    materialize: none          # none（按需计算/读模型内联）| latest（仅最新一份投影）
    refresh: on_demand         # on_demand | scheduled
```

> 口径归属：列级复权（`qfq/hfq`）由**采集/读取层**组合（doc-5 Router），派生引擎消费的是
> 规范化后的输入；派生仅登记真正的计算（因子 / 派生指标）。

规则：

1. **`algorithm_id`**：稳定**审计标识**；算法升级 = **新增 id**（`pe_ttm_v1` → `pe_ttm_v2`），旧 id 与旧实现**永久保留**、可复现，禁止原地覆盖；
2. **`implementation`**：可导入的代码路径；`@register(id, version)` 注册；`meta.algorithm_registry` 由代码与字典生成（id/version/owner/inputs/output/status/生效日，CI 一致）；升级记录事件（effective_from/reason）；
3. **`inputs`**：输入 `dataset.field`；as-of 语义由引擎保证（TASK-3.12）；
4. **`materialize`**：`none`（默认，0 存储：读模型内联或按需计算）｜`latest`（**仅一份**可重建投影，升级→全量重算+代次切换；投影视为缓存，符合 Cache Never Owns Data）；
5. **`refresh`**：`on_demand`（默认）｜`scheduled`（随调度任务刷新）；
6. **不用描述型表达式**作为派生实现；`quality.expression` 仅服务质量规则；
7. **PIT 双维**：`as_of`（输入时点）× `algorithm_id`（默认 active，可 pin 复现）；响应携带 `algorithm_id / inputs as_of / data_generation`；
8. **审计**：派生结果记录 `algorithm_id`（物化投影列/响应元数据），任何数值可回溯到算法版本与输入版本。

CI 校验：`algorithm_id` 全局唯一且不复用；`implementation` 可导入；docstring 含 `Formula/PIT`；`inputs` 存在；`materialize/refresh` 取值合法；历史 id 不得删除。

实现说明（TASK-3.12，落地口径）：

- `@register` 于 `fin_data_platform.derived.registry`；实现必须是模块级函数，`implementation`
  等于函数真实路径（`module.qualname`，防登记漂移）；注册表进程内幂等、同 id 冲突即报错；
- `materialize`/`refresh` 已进入 Pydantic 模型（默认 `none`/`on_demand`）；`algorithm_id` 后缀 `_vN`
  与 `version` 强一致；历史 id 在注册表中永久保留，未被字典引用者同步为 `deprecated`（不删除）；
- `inline_sql`（可选）：算法提供 SQL 模板（输入引用以 `input_view_name` 命名：`cn_equity.daily_bar.close`
  → `cn_equity__daily_bar__close`），引擎把每个输入渲染为 as-of CTE 后产出独立 SQL；
  CI 校验模板覆盖全部 `inputs` 视图名；
- 计算实现可用 DuckDB（引擎只负责 as-of 读取与结果校验；DuckDB 为纯计算引擎，不依赖 PG 扩展）；
- **输入引用与视图命名**：`<dataset>.<field>[@raw|@qfq|@hfq]`（缺省取字典 `adjust.default`）；
  计算视图名 = 引用原文的字符安全化（`.` 与 `@` → `__`，如 `cn_equity.daily_bar.close@raw`
  → `cn_equity__daily_bar__close__raw`），`inline_sql` 必须使用该命名；
- **默认口径已是复权值**：自行计算复权的算法须显式写 `@raw`（否则会二次复权）；
  对不可复权字段声明 `@qfq/@hfq` 由 CI 与运行时一致拒绝；
- **因子输入引用**：输入可为数据字段或**其它因子输出**（`dataset.derived.output`，跨数据集亦可）；
  因子输出不支持 `@mode` 后缀（其口径在登记输入时确定）；**latest 约束**：`materialize=latest`
  的下游要求上游因子亦为 latest，且物化前校验上游投影的算法列与上游指纹
  （`upstream_fingerprint`，不一致报 `upstream_stale`）；
- **依赖门控**：因子依赖自动生成任务依赖（`meta.job_dependencies`），上游成功前下游不入队
  （derive 任务 scope 留空以满足按 `(job_id, scope, window)` 的依赖匹配）。
- 控制面表（修订 0004）：`meta.algorithm_registry` / `meta.algorithm_events` / `meta.data_generation`；
  同步入口 `python -m fin_data_platform.derived --sync|--check|--list`。

## 5. 完整示例：`cn_equity.daily_bar`

```yaml
dataset: cn_equity.daily_bar
semantic_version: 1
domain: cn_equity
description: A 股日线行情（不复权原始价；复权价按 raw + factor、as-of 计算）
pit_class: market
business_key: [entity_id, trade_date]
physical_key: [entity_id, trade_date, knowledge_time, version]
grain: 标的 × 交易日
update_sla:
  frequency: daily
  earliest_available: "T+0 18:00"
  latest_available: "T+0 22:00"
  tolerance: "2h"
sources:
  - provider: tushare
    endpoint: daily
  - provider: baostock
    endpoint: query_history_k_data_plus
coverage:
  universe: A 股（含退市）
  universe_source: cn_equity.listing_lifecycle
  history_start: 1990-12-19
  expected_dates:
    calendar: cn_equity.trade_calendar
    frequency: daily
storage:
  canonical_table: cn_equity.daily_bar
  read_model: mart.equity_daily_bar_v1
  read_model_impl: view            # view | projection_table（doc-13 §1.1）
  partition_strategy: event_time   # event_time | knowledge_time | none（doc-13 §3.1）
  partition_interval: 1 month
  retention: all
  compression:                     # 仅已封口 chunk；不得改变查询语义（doc-13 §3.4）
    after: 7 days
    segment_by: entity_id
    order_by: trade_date
quality:
  - rule: unique
    keys: [entity_id, trade_date]
  - rule: range
    field: volume
    min: 0
  - rule: expression
    expr: "high >= low and low <= close <= high"
    severity: error
  - rule: reconcile
    against: raw_stock_daily
lineage:
  upstream: []
  transform: raw
derived:
  - output: ma20
    algorithm_id: ma20_v1
    implementation: finplatform.derived.factors.ma20
    owner: derived-engine
    inputs:
      - cn_equity.daily_bar.close
      - cn_equity.adj_factor.adj_factor
    description: 前复权收盘价
mappings:
  - provider: tushare
    endpoint: daily
    fields:
      close: close
      volume: vol
      amount: amount
fields:
  - name: entity_id
    type: int64
    nullable: false
    description: 平台标的 ID（实体注册表主键）
    pit_role: none
  - name: trade_date
    type: date
    nullable: false
    description: 交易日
    pit_role: event_time
  - name: open
    type: float64
    unit: 元
    nullable: true
    description: 开盘价（不复权）
    pit_role: none
  - name: close
    type: float64
    unit: 元
    nullable: true
    description: 收盘价（不复权）
    pit_role: none
  - name: volume
    type: float64
    unit: 股
    nullable: true
    description: 成交量
    pit_role: none
  - name: amount
    type: decimal
    precision: 24
    scale: 4
    unit: 元
    nullable: true
    description: 成交额（精确金额）
    pit_role: none
  - name: knowledge_time
    type: timestamp_tz
    nullable: false
    description: 该版本进入平台的时间
    pit_role: knowledge_time
  - name: publish_time
    type: timestamp_tz
    nullable: true
    description: 行情发布/可得时间（若源提供）
    pit_role: publish_time
  - name: ingest_time
    type: timestamp_tz
    nullable: false
    description: 物理入库时间（审计）
    pit_role: ingest_time
  - name: version
    type: int64
    nullable: false
    description: 同业务键的版本号（append-only）
    pit_role: none
  - name: provider
    type: enum
    enum: [tushare, baostock, wind, akshare, fuyao]
    nullable: false
    description: 该行来源（provider 维度）
    pit_role: none
```

## 6. 校验与 CI（强制）

1. 元 schema 校验（Pydantic/JSON Schema 双向）；
2. `semantic_version` 为整数；口径/单位/语义 diff 必须 +1（CI 检测强制）；
3. 命名规范：字段禁源品牌前缀/缩写（Source Independence）；
4. decimal 必须带 `precision/scale`；类型/PIT 角色合法且 `pit_role` 显式（含 `none`）；
5. `business_key` 与 `physical_key` 齐备，`physical_key ⊇ business_key + knowledge_time + version`；
6. `lineage` 必填（源数据显式 `raw`）；血缘无环、上游存在；`derived.inputs` 存在；
7. `quality.expression` 可解析且字段存在；`coverage` 四要素完整可计算；
8. `mappings` 与 v0 归一化 spec 交叉校验（平台 ⊇ 适配器，仅字段名）；换算/枚举映射缺失即为 CI 失败；`storage.read_model` 语义版本化；`storage.partition_strategy` 与 `pit_class` 默认规则一致（覆盖需注释）；
9. 文件路径与 `dataset` 一致（每数据集一文件）；单文件不得包含多个 dataset；
10. `derived`：`algorithm_id` 全局唯一且不可复用；`implementation` 可导入且 docstring 含 `Formula/PIT`；`inputs` 存在；历史 id 不得删除。

## 7. 生成物与消费方

| 消费方 | 生成/校验 |
|---|---|
| 存储层（TASK-3.3） | DDL / Alembic 迁移 / 分区与保留（decimal 精度直出） |
| 派生引擎（TASK-3.12） | 读取 `derived.inputs/output` 注册公式与重算 |
| 归一化层（v0 spec） | `mappings` 交叉校验 |
| SDK / REST（TASK-3.7/3.11） | Pydantic 模型 + OpenAPI |
| WebUI（TASK-3.8） | 字典浏览（字段/口径/血缘/覆盖率） |
| 质量/新鲜度（TASK-3.5/3.6） | quality 规则实例化；SLA → 告警阈值 |

## 8. 变更流程

1. 字典先行 → CI 校验 → 生成/迁移 → 发布；
2. 纯增量（加字段/扩枚举）：不升 `semantic_version`；
3. 语义变更（口径/单位/含义）：`semantic_version + 1` + Read Model `_vN` 并存 + 弃用公告；
4. 删除字段：先弃用（deprecated + 终止日期），过渡后移除。

## 9. 与 v0 归一化 spec 的关系

- v0 `fin_data_hub/specs/*.toml`：**适配器级**响应映射（源字段 → canonical），随 adapter 维护；
- 平台 `mappings`（dataset 级）：平台契约侧的源映射登记；
- 关系：CI 保证"平台 mappings ⊇ 适配器 spec"，两处映射漂移即失败。

## 10. 决策记录（2026-09-13，已定）

| # | 决策 | 理由 |
|---|---|---|
| 1 | 格式 = **YAML** | lineage/coverage/mappings 嵌套深，TOML 可读性不足 |
| 2 | **每数据集一文件**（目录=域） | 单域 50+ DataPanel 大文件无法 review；一文件一条目，diff 最小 |
| 3 | expression 首期仅**比较/逻辑/算术** + 显式字段引用 | 窗口/聚合/join 属派生引擎职责，避免质量 DSL 与引擎双语言 |
| 4 | `mappings` **不含单位换算** | 换算归适配器 spec；字典只描述最终 canonical 语义 |
| 5 | 派生指标 = **代码实现 + `algorithm_id`**（不用描述表达式） | 代码可注释/高效；id 为审计标识，升级新增 id、历史不消失 |
