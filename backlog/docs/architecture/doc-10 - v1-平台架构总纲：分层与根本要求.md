---
id: doc-10
title: v1 平台架构总纲：分层与根本要求
type: specification
created_date: '2026-09-13 12:06'
updated_date: '2026-09-17 12:19'
---
# v1 平台架构总纲：分层、概念与根本要求

> 状态：评审中（第 2 稿） | 关联：doc-2（v1 规划）、doc-5（复权路由）、doc-6（API 契约）、doc-8（对账说明）、TASK-3.1
> 第 2 稿变更：采纳评审意见——新增 DataPanel 精确定义（§3.1）、Raw→Canonical→Read Model 分层（§3.2）、**实体注册表**（§3.3，与 PIT 同级）、Cache 非权威原则（§3.4）、**四时间模型**（§4.1）、**Schema First**（§6.1）；明确 Read Model 为 SDK/REST 唯一读取入口。

## 1. 三层结构（概览）

```
接入层    FinDataHub（Python 库）  唯一 Adapter 访问面；只做归一化，不存数据
           ▲ 仅「采集/回填」可调用

数据层    控制面 | 调度 / 质量检查 / 数据字典与血缘 / 任务与配额观测
          数据面 | 实体注册表 + DataPanel（Raw→Canonical→Read Model）→ TimescaleDB；DuckDB 批量派生
          缓存   | L1 进程内 + L2 Redis（横切；非权威）

服务层    FinDataPlatform（SDK 优先）
          SDK 只读 Read Model（原生 as-of）；REST 薄封装；WebUI 基于 REST；批量导出
```

## 2. 层间依赖与写入边界（硬约束）

1. **单向依赖**：`FinDataHub → 平台写入端 → Canonical → Read Model → SDK/REST/WebUI`；
2. **禁止**：平台直连 Adapter；WebUI 直连 DB/SDK；SDK/REST 直读 Raw/Canonical 物理表；跨层反向依赖；
3. **写入面**仅限平台内部写入端（ingestion / 派生计算 / 文件导入 / 质量结果），按 schema 最小授权，读写 DSN 分离；对外一律无写入。

## 3. 数据层核心概念

### 3.1 DataPanel = 逻辑数据集（Logical Dataset）

**定义**：DataPanel 是**一个有 schema、有 PIT 语义、有字典条目、有质量规则与 SLA 的逻辑数据集**，命名 `{domain}.{dataset}`。

- `DataDomain`（市场平面）= 命名空间：`cn_equity / cn_fund / cn_futures / cn_options / hk_equity / us_equity / us_options / macro_cn / macro_us / macro_global`；
- **宏观必须分域**（避免 `macro` 成为垃圾桶）：区域维度拆为 `macro_cn / macro_us / macro_global`（全球利率/汇率/大宗归 `macro_global`）；若某区域数据集膨胀，用域内 tag 分类，不新增层级、不把异质数据堆入单域；
- DataPanel 是数据域下的**一级实体**：唯一 ID、schema 版本、PIT 类别、主键、更新频率、血缘与质量规则；
- 存储映射（物理表/分区/压缩策略）、派生血缘、读模型与 SDK 方法**均引用 DataPanel ID**；
- **明确排除**：DataPanel ≠ 存储系统 ≠ 数据库 schema ≠ 市场平面 ≠ 物理表集合；主结构只有 **DataDomain → DataPanel** 两级，不引入第三级 `DataSet` 概念（如需分组用 tag/子域，不改主结构）。

示例：

| DataPanel | 说明 |
|---|---|
| `cn_equity.daily_bar` | A 股日线行情（raw + factor 口径） |
| `cn_equity.adj_factor` | 复权因子（版本化） |
| `cn_equity.index_member` | 指数成分（区间型 PIT） |
| `cn_equity.financials.balance_sheet` | 资产负债表（append-only 版本） |
| `cn_equity.market_events.namechange` | 名称变更（闭区间） |
| `cn_fund.nav` | 场外基金净值 |
| `macro_cn.rate` | 中国宏观利率序列 |
| `macro_global.fx_rate` | 全球汇率序列 |

### 3.2 数据分层：Raw → Canonical → Read Model

| 层 | 示例 | 职责 | 写入方 | 读者 |
|---|---|---|---|---|
| **Raw Layer** | `raw_stock_daily` | 源端原始响应落地（源字段与语义不变）；审计、重放、对账；append-only | 采集 | 审计/对账工具（SDK/REST 不可见） |
| **Canonical Layer** | `equity_daily_bar` | 归一化统一资产表：标准字段/单位/枚举/代码 + PIT 字段；跨源合并、口径对齐、复权与派生 | 采集 + 派生 | 派生计算、质量检查、内部读 |
| **Read Model** | `mart.equity_daily_bar_v1`（视图/物化视图） | 面向消费的稳定接口层：字段与口径冻结、语义版本化、as-of 视图、权限裁剪 | 由 Canonical 派生 | **SDK / REST / WebUI / 导出（唯一读取入口）** |

规则：

1. **SDK/REST 只读 Read Model**，不直读 Raw/Canonical 物理表（防耦合）；
2. **换源不影响 SDK**：源差异（Wind/Tushare/AkShare/…）在 Raw→Canonical 之间被吸收，Raw 保留完整溯源；
3. Read Model **语义版本化**（`_v1` / `_v2`），版本号表达**语义契约**而非数据库结构：
   - **字段兼容 ≠ 语义兼容**：新增字段、扩展枚举等增量变更**不升版本**；字段*含义/口径/单位*变化（如 `pe_ttm` 改为 `pe_lyr`、复权基准变化）**必须升主版本**；
   - 升版本 = 新旧并存过渡期 + 弃用公告；`_v1` 冻结后只增不改，移除字段须先弃用；
4. Read Model 是**权威数据的投影**，非缓存：缓存只加速，不改变读取语义（§3.4）。

### 3.3 实体注册表（Entity Registry / Entity Graph；冻结稿修订 2026-09-13）

**定位**：回答"数据说的是谁"——**实体身份、关系与外部标识**。不含交易状态（交易状态属数据集，PIT 事件驱动），不含数据目录（属字典），不含源映射（属 Hub/字典）。

- **覆盖（`entity_type`）**：`issuer / equity / etf / lof / fund / index / bond / future / option / rate / fx / macro / basket`；无实体数据不建实体。
- **分类面（facets，替代 `sec_type`）**：
  - `entity_type`：本体（粗）；
  - `entity_class`：产品细分（`bond_etf / money_etf / reit / equity_index / commodity_future / …`）；
  - `market`：市场面（`cn / hk / us / global`）。
- **表结构（5 张）**：
  - `ref.entity`：`entity_id`、`entity_type/entity_class/market`、`code`（canonical）、`name`、`currency/exchange`、`social_status`（**issuer 专用**：存续/倒闭/重整）、`algorithm_id`（basket）、`valid_from/valid_to`（SCD2 闭区间）、`knowledge_time/version`、`attrs(JSONB，治理留 v1.1)`；物理键 `(entity_id, valid_from, knowledge_time, version)`。
  - `ref.entity_code_history`：canonical 代码履历（代码变更/复用 → 旧码仍可解析）。
  - `ref.entity_relation`：`(entity_id, related_id, relation_type, valid_from/valid_to, knowledge_time, version)`；**单向存储**，双向查询由 `relation_type_dict.inverse_relation` 元数据驱动（零硬编码）。
  - `ref.entity_external_id`：`(entity_id, id_type, id_value, valid_from/valid_to, knowledge_time, version)`；`id_type ∈ isin / figi / cusip / sedol / lei / uscc / other`（**不含 ticker**）。
  - `ref.relation_type_dict`：关系词表（`relation_type / inverse_relation / description`）；新增关系词必须先登记（CI 校验）。
- **Issuer 模型**：`listing --issued_by--> issuer`；`issuer.code` 采用统一社会信用代码（缺失时平台码）；**财务/股东/公司事件类数据集以 `issuer_id` 为键**，行情/成分/复权仍挂 listing。
- **明确不做**：① 交易状态（上市/停牌/ST/退市）不在注册表——由交易状态数据集承载（`cn_equity.listing_lifecycle` + 事件接口 suspension/st），**PIT Universe 由数据集推导**；② `attrs` per-type schema 治理（v1.1）；③ Entity Graph 多跳遍历（v2）。
- **读模型**：`mart.entity_latest_v1`（当前态视图）；as-of 用 `mart.entity_asof(ts)` 表函数或 SDK 构造器（PG 视图不可带参）；SDK/REST 只读 mart。
- **关键流程**：注册/刷新（Hub 基础信息 + namechange 身份属性）→ `resolve(code)`（含旧码）→ `universe(as_of)`（**由生命周期/交易状态数据集推导**）→ 属性 as-of（SCD2）。
- **已落地（TASK-3.15）**：分类面收敛、issuer、relation+字典（词表驱动）、external_id、读模型（`mart.entity_latest_v1` / `entity_asof`）、财务改挂 `issuer_id`、`cn_equity.listing_lifecycle` 数据集。

### 3.4 Cache 非权威原则（Cache Never Owns Data）

1. 缓存（L1/L2）**永远不是权威数据源**：清空后可完全由权威层重建；
2. **禁止仅存于缓存的业务状态**（分布式锁等协调原语除外，且不视为数据）；
3. 键必须 **PIT 安全**（含 as-of / 版本 / 口径维度），失效方式：代际失效 + 按域主动失效；
4. **fail-open**：缓存不可用时直读权威层，正确性不受影响；
5. SDK 直连 DB 模式不经 Redis（可选本地进程缓存）。

### 3.5 派生引擎（Derived Engine；冻结稿修订 2026-09-13）

**原则**：**存输入与算法，不存派生结果的多个版本**；派生输出最多保留"最新一份"可重建投影（缓存性质）。

- **算法版本跟踪**（跟踪代码与元数据，不跟踪数据副本）：
  - `algorithm_id`（升级 = 新 id，**旧实现永久保留**，代码便宜数据贵）；
  - `meta.algorithm_registry`：id / version / owner / inputs / output / status(active|deprecated) / 生效日，由代码 `@register` 与字典生成（CI 一致）；
  - **升级事件**（重述台账）：`algorithm_id / effective_from / reason`，WebUI 可见。
- **三种服务形态**：

| 形态 | 例子 | 存储 | 语义 |
|---|---|---|---|
| 读模型内联（字段级） | `qfq_close` | 0（视图计算） | as-of 输入 × 当前算法 |
| 按需计算（引擎/API） | 自编指数、因子面板 | 0（可选 Redis） | 可 pin `algorithm_id` 复现 |
| 最新投影（可选物化） | 重型派生 | **1 份、可重建** | 升级 → 全量重算 + 代次切换 |

- **PIT 双维语义**：`as_of`（输入知识时点，防前视）× `algorithm_id`（默认当前 active；可 pin 旧版本做审计复现）；响应携带 `algorithm_id / inputs as_of / data_generation`。
- **字典登记**：`derived` 增加 `materialize: none | latest`、`refresh: on_demand | scheduled`（doc-11 §4）。
- **明确不做**：多版本派生数据副本（存储成本）；图算法与复杂因子编排（v2）。

**实现落地（TASK-3.12）**

- 算法登记：代码 `@register(algorithm_id, owner, effective_from?, reason?, inline_sql?)`（`fin_data_platform.derived`）；
  字典 `derived` 校验 `implementation` 可导入且 docstring 含 `Formula/PIT`（CI 三方一致性）；
- 控制面表（修订 0004）：`meta.algorithm_registry`（active/deprecated，历史 id 永存）、
  `meta.algorithm_events`（升级台账）、`meta.data_generation`（代次，格式 `YYYYMMDDTHHMMSSZ`，doc-12）；
- 引擎：as-of 输入（`knowledge_time <= as_of` + 最高 `version` 去重）→ 算法（Arrow 入/出；
  计算实现用 DuckDB，纯计算引擎不依扩展）→ 结果校验（业务键 + output）；pin 历史 `algorithm_id` 复现；
- 读模型内联：算法提供 SQL 模板（输入按 `input_view_name` 命名），引擎渲染 as-of CTE 产出独立 SQL，
  与按需计算共用同一模板（`qfq_close_v1` 为参考实现，含 Formula/PIT docstring）；
- 物化 `latest`：单份投影 `mart.derived_<表>_<output>`，影子表重建 + 事务内原子换名，
  升级即 pin 新 id 重跑换代次（可重建缓存，Cache Never Owns Data）。

## 4. PIT：四时间模型

### 4.1 四个时间

| 时间 | 含义 | 典型场景 |
|---|---|---|
| `event_time` | 事件发生/归属时间 | 交易日、报告期末 `report_period`、除权日 |
| `publish_time` | **官方/权威发布时间** | 上市公司公告时间、指数公司公布时间 |
| `knowledge_time` | **平台获知时间**（该版本首次进入平台） | 采集完成时刻 |
| `ingest_time` | 物理入库时间（审计/运维） | DB 写入时刻 |

说明：`publish_time` 与 `knowledge_time` 必须分开——公告 21:00 发布、平台次日 02:00 采集，两者语义不同；只存其一会在后期产生口径争议与补救成本。

### 4.2 查询与版本语义

- as-of 默认：`knowledge_time <= as_of` 后取每键最新版本；
- 需要"官方可得"更严语义时：`publish_time <= as_of`（且记录 `knowledge_time >= publish_time`）；
- 不传 as_of = 当前 `is_latest` 视图；
- append-only + `version` + `is_latest`；更正不回写历史；重述以新版本表达。`is_latest` 为**读侧派生**（Read Model 计算/物化），Canonical 只存 `version / knowledge_time`（doc-13 §4）。

### 4.3 PIT 三要素（缺一不可）

1. **完整宇宙**：含退市标的；**universe 由交易状态数据集推导**（`cn_equity.listing_lifecycle` 上市/退市 + 停牌/ST 事件），名称变更（身份属性）在注册表按 SCD2 留痕；
2. **区间与版本**：属性 SCD2、成分 in/out、财务 append-only 版本；
3. **as-of 计算纪律**：复权价按 as-of 基准日由 raw + factor 计算（不落全量 qfq 快照）；派生数据以 as-of 输入计算并支持重述重算；**禁止前视 join**（按知识/发布时间过滤）。

## 5. 根本要求

### 5.1 数据准确

- 约束检查（唯一/完整/范围/枚举/引用）；跨源对账（TASK-4.1，方法见 doc-8）；
- 派生公式与依赖登记于数据字典，重算可复现；质量结果入库并生成报告（WebUI 可浏览、分级告警）。

### 5.2 数据新鲜（可度量）

- 每数据集定义 **SLA**：更新频率 + 最早/最晚可用时点 + 容忍延迟；
- 采集侧记录 `job_runs` 与 **watermark**（最近成功数据时点/知识时点）；
- 指标：`last_success_at`、数据龄 lag、覆盖率、缺口数；
- 分级告警 + WebUI 新鲜度面板。

## 6. 架构原则

### 6.1 Schema First：Dictionary → Schema → SDK/API

- **数据字典（机读）是唯一事实源**：数据集/字段/类型/单位/PIT 类别/约束/血缘/派生公式/覆盖率/SLA；
- DDL、归一化 spec、SDK 模型、REST schema、文档**由字典生成或经 CI 强校验一致**；
- 变更流程：字典 → schema → 代码/迁移 → 弃用公告；**禁止"代码先写、文档后补"**；
- 平台生命周期以 5~10 年计，Schema First 的长期收益远大于成本。

### 6.2 Source Independence Principle（源独立性）

**任何 DataPanel / Canonical / Read Model 不得暴露供应商特有字段或语义**：

- ❌ `wind_ind_code` → ✅ `industry_code` + `industry_provider`；
- ❌ `ts_pe` → ✅ `pe_ttm`（口径写进字典，来源写进血缘/provider 维度）；
- 源特有信息只能以两种形式存在：① 通用字段 + `provider`/`scope` 维度；② **源扩展表**（仅 Raw 层或独立 extension schema，不进入 canonical 公共 schema）；
- **Raw Layer 例外**：允许原样保留源字段（审计/重放所需）；
- 目的：防止 Canonical Layer 退化为"某厂商层"，保证换源、多源合并与 SDK 长期稳定；
- 强制校验：数据字典 CI 检查字段命名（禁止源品牌前缀/缩写进入 canonical），provider 必须可显式追溯。

### 6.3 依赖与写入硬约束

同 §2：单向依赖、对外无写入、内部写入端最小授权。

### 6.4 成本与权限

付费源按调用计量；凭证三面（内部写入 / SDK 只读 DB 角色 / REST API Key `read/export/admin`），不落镜像。

## 7. 补充要求

- **可审计/可复现**：血缘 + 版本 + 口径登记，任一对外数值可回溯至源与计算过程；
- **可扩展**：市场平面插件化（DataDomain 新增不改核心）；DuckDB 批量派生与扫描（非缓存、非真源）；
- **部署**：单机 Docker Compose（已定），无需 K8s。

## 8. 任务落位映射

| 层/概念 | 任务 |
|---|---|
| 数据字典 / Schema First | TASK-3.2（规范：doc-11） |
| 存储（Raw/Canonical/Read Model 落地、分区迁移） | TASK-3.3 |
| 实体注册表（Entity Registry） | TASK-3.14 |
| 质量与新鲜度 SLA | TASK-3.5 |
| 调度与 watermark | TASK-3.6 |
| Redis 缓存（非权威） | TASK-3.9 |
| 派生计算与重述 | TASK-3.12 |
| 时序查询（as-of/频率/缺口/vintage） | TASK-3.13 |
| REST / WebUI / 导出 / SDK 发布 | TASK-3.7 / 3.8 / 3.10 / 3.11 |
| 部署 | TASK-3.4 |
| 对账框架 | TASK-4.1 |
