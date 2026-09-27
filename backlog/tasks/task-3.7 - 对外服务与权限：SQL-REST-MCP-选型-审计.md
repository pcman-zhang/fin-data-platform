---
id: TASK-3.7
title: FinDataPlatform REST（SDK 薄封装）与权限审计
status: In Progress
assignee: []
created_date: '2026-09-13 06:01'
updated_date: '2026-09-27 20:14'
labels: []
milestone: m-0
dependencies:
  - TASK-3.1
  - TASK-3.3
parent_task_id: TASK-3
ordinal: 26000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
在 SDK 之上实现 REST 薄封装（版本化 + OpenAPI）：直接调用 FinDataPlatform SDK 查询（含 PIT/as-of 参数），不重复实现语义；游标分页与字段裁剪、压缩（gzip/zstd）、可选 Arrow 列式响应、ETag/If-None-Match、批量端点与异步导出触发；**API Key 鉴权与作用域（read/export/admin；不提供数据写入）**、按 key 限流与成本归因、调用审计；接口文档与数据字典联动；SLO：小批量在线查询 P95 ≤ 200–500ms。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 服务形态按设计落地并通过联调；压缩/分页/字段裁剪/Arrow 响应可用
- [ ] #2 接口文档与数据字典联动；SLO 有基准测试
- [ ] #3 首期不提供 API Key 鉴权与调用审计（用户决策 2026-09-28：个人平台定位；认证授权与审计留待增强，见 doc-15 / doc-16）
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
评审通过（2026-09-28），分支 feat/task-3.7-rest；范围决策：首期不提供 API Key 鉴权与调用审计（原 AC#2 相应调整）。
1. 查询内核（新模块 query/，供 REST 与未来 SDK 复用，不依赖 FastAPI）：version_mode=latest/as_of/history；as_of_policy=knowledge/publish（fallback_mode strict/allow）；fields 裁剪；filters 结构化 AST（op 白名单）；keyset 游标（order_by + 业务键 + 物理键）；limit 默认 1000 / 上限 50000。
2. 数据面路由：GET /v1/datasets/{dataset}/rows（canonical PIT 行）；GET /v1/raw/{dataset}/rows（访问面：adjust 缺省取字典、align_calendar；复用 access.read，异常码原样映射）；GET /v1/factors/{output}/rows（严格 as_of 对齐、algorithm_id pin；复用 factor_api.read）；GET /v1/datasets/{dataset}/schema、/v1/freshness（水位/滞后/覆盖率）、/v1/health、/v1/entities/{id}/aliases。
3. 传输与协议：响应头（X-As-Of/X-Version-Mode/X-Dataset/X-Semantic-Version/X-Data-Generation/X-Freshness-Lag/X-Query-Rows/X-Query-Cost/X-Cache/X-Request-Id/ETag）、If-None-Match→304、gzip 压缩、Arrow IPC（Accept / format 协商）、RFC 9457 错误模型（含访问面错误码与可执行提示）。
4. 基准：SLO 基准测试（小批量在线查询 P95 ≤ 500ms，离线基线；PG 集成脚本）。
5. 测试与文档：TestClient 语义测试（三模式/fallback/filters/游标/裁剪/ETag/304/压缩/Arrow/错误码/freshness）；docs/configuration.md §7、docs/sdk.md、doc-12 同步（实现路径、无鉴权）、README（收尾）。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
分层（2026-09-13）：REST 仅封装 FinDataPlatform SDK；WebUI 依赖本层（TASK-3.8 已加依赖）。

REST DTO 使用 FinDataPlatform SDK 的 Pydantic 模型（FastAPI/OpenAPI 同一来源，doc-2 §6.17）。

实施进展（2026-09-28，分支 feat/task-3.7-rest）：① 查询内核 query/（REST 与未来 SDK 共用）：version_mode（latest/as_of/history）、as_of_policy（knowledge/publish + fallback strict/allow）、fields 裁剪、filters 结构化 AST（op 白名单）、keyset 游标（order_by+业务键+物理键、方向感知）、limit（1000/50000）、include_meta（版本列）；错误码按 doc-12 §5（结构化 code/hint）。② 数据面路由：GET /v1/datasets/{dataset}/rows、/v1/raw/{dataset}/rows（复用 access.read：adjust/日历对齐）、/v1/factors/{output}/rows（复用 FactorAPI：严格 as_of、algorithm_id pin）、/v1/datasets/{dataset}/schema（字段 JSON Schema）、/v1/freshness（水位/滞后/质量覆盖）、/v1/health、/v1/entities/{id}/aliases（代码履历+外部标识）。③ 传输：响应头（X-As-Of / X-Version-Mode / X-Semantic-Version / X-Data-Generation / X-Freshness-Lag / X-Query-Rows / X-Query-Cost / X-Cache / X-Request-Id / ETag）、If-None-Match→304、gzip、Arrow IPC（Accept / format 协商）、RFC 9457 错误处理器（QueryError / AccessError / FactorError）。④ 首期无鉴权/审计（AC#2 已按决策调整）；因子与质量/任务一致走写连接（依赖 meta 算法登记）。⑤ 测试：tests/test_platform_rest_rows.py 10 项（三模式 / 发布语义 strict+allow / 过滤 / 游标 / 投影 / ETag+304 / gzip / Arrow / 错误码 / raw 对齐与 adjust / schema / aliases / freshness / SLO 基线 p95≈77ms）；全量 pytest EXIT=0、ruff、mypy(147 文件) 全绿。⑥ 文档：configuration §7（数据面端点与传输、无鉴权说明）、sdk.md 状态、components.md、README（新增数据面 REST 行；消费层出口去掉「正式 REST」）、doc-12 §11 实现说明。

真实部署复验（2026-09-28，docker compose 新镜像 + 真实数据）：PIT 行（latest / as_of / keyset 游标分页）✅；Raw（adjust=raw 1251.24 vs hfq 10818.60，factor_dataset=cn_equity.adj_factor）✅；因子 ma20（已物化，X-Data-Generation=20260920T072931Z、algorithm_version=1）✅；schema（13 字段 / 6 必填）、freshness（lag=0、覆盖率 1.0）、aliases ✅；ETag→304、gzip、Arrow IPC（83 行 × 3 列）✅；RFC 9457 错误体（version_mode_required 带 hint/request_id）✅；SLO（PG，curl 端到端 20 次小查询）p50=182ms / p95=197ms（目标 ≤200–500ms）。

独立代码评审（2026-09-28，第二轮；评审代理 + 自审）发现并修复（均补测试，REST 测试 19 项）：① 【高】ETag 未覆盖 entity_id / 窗口 / limit / cursor / 协商格式 → 跨查询 304 返回错误正文；已纳入全部决定响应的参数。② 【高】数据版本令牌「水位优先」漏掉重述/补数（不推进水位）→ 陈旧 304；已改为「水位 ∪ 最近成功运行」（平台写入一律经任务执行；带外写库不保证失效，doc-12 注明）。③ 【高】可空排序列边界行含 NULL 时自产游标不可消费（500）；已改为不给游标 + warnings 提示；游标值类型/日期严格校验（422）。④ 【中】未登记因子 / 非法 dataset → 500；已新增 UnknownFactor / AmbiguousFactor 结构化错误（404 / 422）。⑤ 【中】filters / 游标值类型非法 → 500；已按列类型校验/转换（422）。⑥ 【中】RFC 9457 覆盖：数据面统一 application/problem+json + X-Request-Id；框架级参数校验在数据面走问题体（非数据面保持 FastAPI 默认）；schema / aliases 404 改为问题体。⑦ 【中】/rows 与 access 的版本选择口径不一致；已对齐（version 优先）。⑧ 【中】scd2 未按 pit_class 门控；已与访问面一致报 unsupported_pit_class。⑨ 【中】include_meta=false 且 fields 仅含版本列 → rows 空但 row_count 非 0；已报 invalid_field。⑩ 【低】is_null 缺省语义、format 非法静默降级、history+as_of 静默忽略、新鲜度丢弃未采集数据集、每请求元数据构建、连接口径文档措辞 —— 均已修。验证：全量 pytest EXIT=0、ruff、mypy(147 文件) 全绿。

评审修复部署复验（2026-09-28，docker compose 新镜像）：ETag 按 entity_id 区分（10001/10002 不同；同参数仍 304）✅；未知因子 → 404 unknown_factor（application/problem+json + X-Request-Id）✅；format=xml → 422 ✅；ref.entity（scd2）→ 422 unsupported_pit_class ✅；limit=0 → invalid_query 问题体 ✅；is_null 过滤生效 ✅；/v1/freshness 列数据集全集（16 个，13 个无水位）✅；SLO 复测 p50=20.6ms / p95=22.1ms（元数据进程内缓存后较修复前 182ms 大幅改善）。
<!-- SECTION:NOTES:END -->
