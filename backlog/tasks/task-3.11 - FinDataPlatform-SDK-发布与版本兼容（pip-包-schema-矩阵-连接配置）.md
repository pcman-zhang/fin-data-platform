---
id: TASK-3.11
title: FinDataPlatform SDK 发布与版本兼容（pip 包 / schema 矩阵 / 连接配置）
status: In Progress
assignee: []
created_date: '2026-09-13 06:29'
updated_date: '2026-10-07 10:46'
labels: []
milestone: m-0
dependencies:
  - TASK-3.24
  - TASK-3.25
  - TASK-3.26
parent_task_id: TASK-3
ordinal: 30000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
SDK 作为独立发行物：pip 安装；**双模式（直连只读副本/读模型 DB 凭证；或 REST 后端 + API Key）**；连接配置注入（禁止内部原始表）、PIT/as-of 参数与结果元数据（source/as_of/quality）、SDK 版本 ↔ DB schema 版本兼容矩阵与连接校验、读模型弃用流程与版本同步。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 SDK 可 pip 安装并直连只读副本/读模型（只读角色）；连接配置注入且不落仓库
- [ ] #2 PIT/as-of 参数与结果元数据契约落地；与 REST 结果语义一致
- [ ] #3 SDK 版本与 schema 版本兼容矩阵 + 连接校验（不兼容明确报错）
- [ ] #4 读模型变更走弃用流程并同步 SDK 版本说明
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
评审通过（2026-10-07），分支 feat/task-3.11-sdk；范围决策：REST 模式无 API Key（延续 3.7）；直连控制面意图需 control_dsn；REST 模式暂不支持 panel；兼容区间 [0006_daily_status, 0007_quality_meta]。
1. sdk/ 新模块：config（SdkConfig + from_env，凭证只注入）、errors（FinDataError，映射各层结构化异常 + REST problem+json）、models（结果元数据 Pydantic + JSON Schema 导出）、compat（版本↔schema 兼容区间 + 连接校验）、direct（复用 access/FactorAPI/query/panel/ControlClient）、rest（httpx 调 /v1 数据面与控制面）、client（FinDataPlatform 门面：raw/factors/read_model/panel/control；结果 .frame/.table/.meta）。
2. 打包：新增 sdk extra（httpx）。
3. 兼容与弃用：compat 常量为单一事实源；docs/sdk.md 增「版本与兼容 / 弃用流程」。
4. 测试：tests/test_platform_sdk.py（直连 sqlite / REST ASGITransport / 元数据同构 / 错误映射 / 兼容校验 / 配置注入）。
5. 文档：docs/sdk.md、docs/configuration.md（FDP_SDK_*）、README（消费层出口：SDK ✅ / 批量导出 🚧）。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
发行物命名（2026-09-13）：SDK 包名 fin-data-platform，依赖 fin-data-hub（FinDataHub）。

SDK 接口模型采用 Pydantic v2；导出 JSON Schema 作为契约文档（doc-2 §6.17）。

实施进展（2026-10-07，分支 feat/task-3.11-sdk）：① sdk/ 模块：client（门面 raw/factors/read_model/panel/control；结果 .frame/.table/.meta）、direct（复用 access/FactorAPI/query/panel/ControlClient；engine 可注入）、rest（httpx 调 /v1；问题体解析 + 非问题体错误映射）、config（SdkConfig/from_env；DATABASE_READ_* 优先）、models（ResultMeta/FactorResultMeta/ControlRunInfo/ErrorModel + export_json_schema）、compat（SDK_VERSION ↔ [0006_daily_status, 0007_quality_meta]；直连读 alembic_version、REST 用 /v1/health 自检）、errors（FinDataError）。② 打包：pyproject 新增 sdk extra（httpx）。③ 测试：tests/test_platform_sdk.py 5 项（配置注入与校验 / 兼容区间 / 直连读取与错误映射 / 直连控制面 control_dsn 门控 / REST 与直连元数据同构 + 问题体错误映射 + panel 限制）；全量 pytest EXIT=0、ruff、mypy(155 文件) 全绿。④ 文档：docs/sdk.md §6/§7（安装与双模式、版本与兼容/弃用）、configuration §7.1（FDP_SDK_*）、README（SDK ✅ / 批量导出 🚧）、doc-21 §8 实现说明。

独立代码评审（2026-10-07，第二轮）发现并修复（SDK 测试 14 项）：① 【高】[sdk] extra 依赖不全（REST-only 也需全平台依赖）→ extra 补齐（pydantic/pyyaml/sqlalchemy/psycopg/pyarrow/duckdb）+ DirectBackend/RestBackend 延迟导入（REST-only 仅需 pandas/pydantic/httpx）。② 【高】只读角色无 public.alembic_version SELECT → 直连连接校验失败 → 授权脚本显式最小授权（仅版本字符串）+ 集成测试断言。③ 【高】REST trigger(window=...) 被服务端静默忽略 → SDK 显式拒绝（unsupported_in_rest_mode；窗口=触发日）。④ 【中】httpx 传输异常/非 JSON 响应 → FinDataError(upstream_unavailable)。⑤ 【中】REST 空结果丢失列集合 → 响应 meta 增加 columns（数据面三端点）+ SDK 空帧按 columns/fields/schema 保留列。⑥ 【中】直连 filters 非法 → KeyError 泄漏 → 结构化 unsupported_filter（与 REST 同码）。⑦ 【中】直连 ensure 先提交后校验 → 前置校验（0 个 → job_not_registered；>1 → invalid_request；水位已追平 → invalid_request 统一）。⑧ 【中】REST wait 丢 created/note → 保留；run_id≤0 直接报错。⑨ 【中】REST 兼容校验未用 SDK 区间 → /v1/health 暴露 schema_revision（值），REST 侧复用 check_schema_revision。⑩ 【中】DSN 组装与 StorageConfig 不一致（READ_HOST/PORT/NAME 丢失、/ 未转义）→ 修正 + 单测。⑪ 【低】FactorResultMeta 增 warnings；raw/factor 增 limit（两模式截断+告警）；close()/上下文管理器；aware as_of 归一；_fallback_error 细化；intent_error → invalid_request 同构；pyproject 版本源改为 fin_data_platform._version。验证：全量 pytest EXIT=0、ruff、mypy(155) 全绿；集成授权测试（真实 PG，含 alembic_version 可读断言）通过。
<!-- SECTION:NOTES:END -->
