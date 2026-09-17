---
id: doc-12
title: FinDataPlatform REST 契约与 PIT（as-of）语义
type: specification
created_date: '2026-09-13 12:28'
updated_date: '2026-09-17 14:32'
---
# FinDataPlatform REST 契约与 PIT（as-of）语义

> 状态：**已冻结**（2026-09-13，第 3 稿） | 关联：doc-10 §1.3/§3.2/§4、doc-11（数据字典）、TASK-3.1（AC#6）/ TASK-3.7
> 第 3 稿变更（冻结稿）：`version_mode` 改为**必选枚举**（缺失 422 `version_mode_required`）；补 `X-Data-Generation` 定义与格式（Read Model 构建代次）。
> 第 2 稿变更（采纳评审）：① 删除 `/latest`，统一 `rows`（PIT 语义唯一入口）；② `include_history:bool` → **`version_mode`（latest / as_of / history）**；③ publish 缺 `publish_time` 不再隐式回退——新增 **`fallback_mode: strict|allow`（默认 strict）**；④ filters 结构化 AST；⑤ cursor 泛化为 `order_by + business_key + physical_key`；⑥ ETag 纳入 `read_model_version / data_generation`；⑦ 派生数据集 `algorithm_id` 始终返回；⑧ 响应头增加 Query Cost；⑨ 预留研究快照端点。定位不变：REST = SDK 薄封装、只读、Read Model 唯一数据面。

## 1. 原则

1. **SDK 优先**：REST 与 SDK 共用 Pydantic 模型与同一查询内核，语义一致；
2. **只读**：数据面无写入端点；管理动作仅 `admin` 作用域控制面条目；
3. **只读 Read Model**：路由以 `dataset` 表达，不暴露物理表/任意 SQL；
4. **PIT 严格**：三种版本模式显式选择；**禁止隐式语义替换**（宁可报错）；
5. **在线小查询定位**：大面板/回测走导出/只读副本（doc-2 §6.4）；
6. **PIT 安全缓存**：Redis 键含 `version_mode / as_of / policy / 字段 / semantic_version / data_generation`。

## 2. 资源与路由（`/v1`）

### 2.1 元数据

| 方法 | 路由 | 说明 |
|---|---|---|
| GET | `/v1/datasets` | 数据集列表（id / semantic_version / SLA / read_model） |
| GET | `/v1/datasets/{dataset}` | 字典详情（含派生数据集的 `algorithm_id`） |
| GET | `/v1/datasets/{dataset}/schema` | 字段 JSON Schema（机器可读） |
| GET | `/v1/freshness` | 新鲜度（last_success_at / lag / 覆盖率） |
| GET | `/v1/health` | 健康检查 |

### 2.2 数据（dataset-generic，PIT 唯一入口）

| 方法 | 路由 | 参数（核心） |
|---|---|---|
| GET | `/v1/datasets/{dataset}/rows` | `entity_id`（可重复）、`start/end`、`version_mode`、`as_of`（as_of 模式必填）、`as_of_policy`、`fallback_mode`、`fields`、`filters`（结构化）、`order_by`、`limit`、`cursor`、`format` |
| GET | `/v1/entities/{entity_id}` | 实体注册表基础信息 |
| GET | `/v1/entities/{entity_id}/aliases` | 多源代码映射（含有效期） |

> **不设 `/latest`**：`version_mode=latest` 即"当前最新"；`version_mode=as_of` + `as_of=now` 即"当前时点可见"。单一入口，避免两套语义漂移。

### 2.3 复杂查询、导出与快照

| 方法 | 路由 | 说明 |
|---|---|---|
| POST | `/v1/exports` | 异步导出（dataset + filters + fields + Parquet/Arrow）→ `job_id` |
| GET | `/v1/exports/{job_id}` | 导出状态与下载 |
| GET | `/v1/snapshots/{snapshot_id}` | **研究快照（预留，v1.1+）**：冻结"dataset 集 + as_of + 过滤"的结果视图 |
| POST | `/v1/admin/jobs/sync` | 控制面：触发采集/回填（仅 `admin`） |

> 首期不开放任意查询接口（无 `POST /v1/query`）；复杂分析走导出、DuckDB 与只读副本。

### 2.4 SDK ↔ REST 映射（示例）

| SDK | REST |
|---|---|
| `get_bars(...)` | `GET /v1/datasets/cn_equity.daily_bar/rows?...` |
| `get_financials(kind=balance_sheet)` | `GET /v1/datasets/cn_equity.financials.balance_sheet/rows` |
| `get_market_events(kind=namechange)` | `GET /v1/datasets/cn_equity.market_events.namechange/rows` |
| `get_entity_info(...)` | `GET /v1/entities/{entity_id}` |
| `raw.read(dataset, *, adjust=...)`（访问面，草案） | `GET /v1/raw/{dataset}/rows?...`（随 TASK-A 定稿） |
| `factors.read(output, *, as_of=...)`（访问面，草案） | `GET /v1/factors/...`（随 TASK-B 定稿） |
| `control.ensure(...)` / `control.materialize(...)`（控制面意图） | `POST /v1/jobs/*`（同 `/v1/jobs/sync` 的意图模式，随 TASK-C 定稿） |

> 已定（§10-1）：dataset-generic——SDK 保持语义化方法，REST 保持资源化，避免数百个 typed endpoint。
> 访问面语义：Raw 读取 = Canonical + **口径组合**（`adjust`，缺省取字典声明）；Factor 读取 =
> **严格 as_of 对齐**（不对齐抛异常，不做 lazy 回填）；回填/物化一律为控制面意图（平台执行，SDK 无写权限）。
> SDK 完整契约见 doc-21。

## 3. PIT / as-of 语义

### 3.1 参数

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `version_mode` | `latest` / `as_of` / `history` | **必填** | 显式选择，无默认（缺失 → 422 `version_mode_required`）；`history` 需受控 scope |
| `as_of` | ISO8601（带时区） | — | **`version_mode=as_of` 时必填**（缺省即 422，不隐式取 now） |
| `as_of_policy` | `knowledge` / `publish` | `knowledge` | publish 语义见 §3.3 |
| `fallback_mode` | `strict` / `allow` | **`strict`** | `publish_time` 缺失时：`strict` → 422；`allow` → 回退 knowledge 且响应显式标注 |
| `include_meta` | bool | false | 控制版本类列（`knowledge_time / publish_time / version / provider`）；**派生数据集的 `algorithm_id` 不受此开关影响，始终返回** |

### 3.2 版本模式（三种，无 `/latest` 特例）

| 模式 | 语义 | 典型用途 |
|---|---|---|
| `latest` | 返回 `is_latest` 当前最新版本；忽略 `as_of` | 在线看板/选股 |
| `as_of` | 返回每个业务键满足 `knowledge_time <= as_of` 的**最新可见版本**；`as_of` 必填 | 回测、研究、Agent 问答 |
| `history` | 返回**全部版本**（append-only），含 `version`；需受控 scope（审计） | 审计、重述核对 |

补充：`as_of` + `as_of_policy=publish` 时按 `publish_time <= as_of` 过滤（官方可得语义）。

### 3.3 publish 语义与回退（不隐式替换）

1. `as_of_policy=publish` 且行 `publish_time` 缺失：
   - `fallback_mode=strict`（默认）：返回 `422 publish_time_missing`（**宁可报错**）；
   - `fallback_mode=allow`：回退 knowledge，响应头 `X-Publish-Fallback: knowledge` + body `warnings[]`，并记录审计；
2. 因子/复权：复权价按所选模式的因子版本由 raw + factor 计算，响应含 `factor_ref{dataset, as_of, provider}`；
3. 派生指标：响应始终含 `algorithm_id`（doc-11 §4），可回溯算法版本。

### 3.4 响应元数据

- 响应头：`X-As-Of`、`X-Version-Mode`、`X-Dataset`、`X-Semantic-Version`、`X-Read-Model-Version`、**`X-Data-Generation`**（Read Model 构建代次，格式 `YYYYMMDDTHHMMSSZ`，如 `20260913T153000Z`；ETag/审计/缓存/排障共用）、`X-Freshness-Lag`、`X-Query-Rows`、`X-Query-Cost`、`X-Cache`（HIT/MISS）、`X-Request-Id`、`ETag`
- 响应体 `meta`：`{dataset, version_mode, as_of?, policy, fallback?, semantic_version, data_generation, row_count, coverage?, warnings[], generated_at, next_cursor?}`

## 4. 过滤、分页、传输、缓存键

### 4.1 filters（结构化 AST，GET/POST 统一）

```json
{"filters": [
  {"field": "trade_date", "op": "between", "value": ["2024-01-01", "2024-12-31"]},
  {"field": "entity_id", "op": "in", "value": [10001, 10002]},
  {"field": "status", "op": "eq", "value": "ACTIVE"}
]}
```

- `op` 白名单：`eq / ne / in / between / gt / gte / lt / lte / is_null`；
- GET 使用同一 AST 的 URL 编码形式，**内部统一解析**（禁止各端点自定义 query parser）；
- 禁止任意表达式/SQL；字段必须存在于字典（CI 校验）。

### 4.2 游标（泛化，不写死组合）

```
cursor = (order_by, business_key, physical_key)
```

- `order_by` 默认 = 业务键升序；游标编码 `order_by` 与最后一行的业务键 + 物理键；
- 覆盖 `index_member` 等非 `(entity_id, trade_date)` 主键的数据集。

### 4.3 ETag / 缓存键（含数据代次）

```
etag = hash(dataset + version_mode + as_of + policy + fallback + filters + fields
            + order_by + semantic_version + read_model_version + data_generation)
```

- `data_generation`：Read Model 构建代次（响应头 `X-Data-Generation`，时间戳格式）；每次重算/算法升级/回填刷新递增（**防算法升级后 ETag/缓存不失效**）；审计与排障按代次定位；
- Redis 键同上构成；缓存命中/未命中以 `X-Cache` 返回；fail-open 直读数据库。

### 4.4 传输与限制

- 游标分页（禁大偏移 `offset`）；`limit` 默认 1000、上限 50000；
- `fields` 裁剪；压缩 `gzip/zstd`；Arrow IPC（`Accept: application/vnd.apache.arrow.stream`）；`If-None-Match` → 304；
- 全部端点无副作用、幂等可重试。

## 5. 错误模型（RFC 9457）

```json
{"type":"...","title":"...","status":422,"detail":"...","instance":"...",
 "request_id":"...","dataset":"...","version_mode":"as_of","as_of":"..."}
```

错误码：`invalid_dataset / invalid_field / version_mode_required / invalid_version_mode / as_of_required / invalid_as_of / publish_time_missing / unsupported_filter / not_found / forbidden_scope / rate_limited / upstream_unavailable`。

访问面（Raw / Factor）追加：`unsupported_adjust / factor_not_materialized / as_of_not_aligned / window_not_covered / inputs_stale`；
每项附可执行提示（触发哪类回填 / 物化任务、或改用按需因子）。

## 6. 鉴权 / 审计 / 限流 / 计量

- **API Key**：哈希存储、可撤销；scopes `read / export / admin`；`history` 模式需附加受控 scope（如 `audit`）；
- **审计**：key/scope、dataset、version_mode、as_of、fallback、row_count、cost、latency、request_id；
- **限流**：按 key × 端点（Redis），429 + `Retry-After`；
- **计量与成本**：`X-Query-Cost` 输出查询成本估算（扫描行/字节/上游调用），供配额、计费与慢查询治理复用（首期先出 headers，规则细化归 TASK-3.10/3.11）。

## 7. 版本化与兼容

- API `/v1`；dataset `semantic_version` 与 `read_model_version`、`data_generation` 均在响应头；
- Read Model `_vN` 对调用方透明（URL 不变）；
- 字段弃用：schema 标注 `deprecated + sunset`；语义变更 `semantic_version+1`，新旧并存过渡。

## 8. SDK 直连模式一致性

- 与 REST：同一 version_mode/as_of 语义、同一列集（含 `algorithm_id`）、同一错误语义（含访问面异常码）；
- 访问面（Raw / Factor）三种模式同源：SDK 直连、REST、平台内部调用共用同一实现（`access` 层）；
- 直连不经 Redis（可选进程内缓存）；模式切换仅改连接配置。

## 9. 明确不做（边界）

- 无写入/DDL；无任意 SQL/表达式；无跨数据集 join；无实时推送（doc-2 §6.15 预留）；
- 不承担大面板回测；研究快照能力预留（v1.1+）。

## 10. 决策记录（2026-09-13）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 路由风格 | **dataset-generic**（SDK 语义化 / REST 资源化） |
| 2 | `POST /v1/query` | **首期禁止**（导出 + DuckDB + 只读副本承担复杂查询） |
| 3 | `include_meta` 默认 | false（派生 `algorithm_id` 不受影响，始终返回） |
| 4 | publish fallback | **`fallback_mode` 显式，默认 `strict`（422）**；`allow` 时才回退并标注 |
| 5 | PIT 入口 | 删除 `/latest`，统一 `rows` + `version_mode` |
| 6 | `version_mode` | `latest / as_of / history` 三模式；`as_of` 必填不隐式取 now |
| 7 | filters/cursor/ETag | 结构化 AST；cursor = order_by+业务键+物理键；ETag 含 read_model_version + data_generation |
| 8 | Query Cost / Snapshot | Cost headers 随首期；研究快照端点预留 v1.1+ |
