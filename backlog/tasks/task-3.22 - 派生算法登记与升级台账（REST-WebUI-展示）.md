---
id: TASK-3.22
title: 派生算法登记与升级台账（REST + WebUI 展示）
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-17 13:48'
updated_date: '2026-09-17 13:58'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
ordinal: 61000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
doc-10 §3.5 要求算法升级事件（重述台账）WebUI 可见；doc-14 信息架构 #7「派生与算法」。本任务只读：REST 暴露算法注册表 / 升级台账 / 投影代次，WebUI 新增「算法」页展示。
<!-- SECTION:DESCRIPTION:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. store 增 list_generations()（+ 内存实现）；API 上下文注入 AlgorithmStore
2. REST（只读，写连接读 meta）：GET /v1/algorithms（注册表）、/v1/algorithms/events（升级台账）、/v1/algorithms/generations（投影代次）+ Pydantic 响应模型 + OpenAPI
3. WebUI：「算法」导航页（注册表 Table[状态 Badge/主键 code/枚举 Tag/长文本 ellipsis+Tooltip]、升级台账 Timeline、投影代次 Table）+ api.ts 类型与客户端
4. web/DESIGN.md：§5 登记导航图标（FunctionOutlined）、§9.17 Menu 项、§9.20 选型表补充
5. 测试与验证：API 单测（含空态）；WebUI 构建 + headless 渲染（空/有数据）；栈内实测（重建镜像 + :8000）
6. 文档：doc-14 §5 接口路径对齐实现（/v1/algorithms*）
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（未提交）：① REST 只读三端点 GET /v1/algorithms（注册表）、/v1/algorithms/events（升级台账）、/v1/algorithms/generations（投影代次），经控制面写连接读 meta（只读角色不含 meta），Pydantic 响应模型 + OpenAPI 收录；ApiContext 注入 AlgorithmStore（SqlAlgorithmStore(writer_engine)）；derived/store 增 DataGenerationRow 与 list_generations（内存/SQL，内存实现记录 updated_at）。② WebUI 新增「算法」导航页：注册表 Table（主键 Typography code / 状态 Badge active-deprecated / 长文本 ellipsis+Tooltip / inputs 换行 tooltip）、升级台账 Timeline（按 effective_from 倒序）、投影代次 Table；两处空态含下一步说明。③ web/DESIGN.md 登记：§5 图标 FunctionOutlined、§9.17 五项导航、§9.20 台账 Timeline。④ doc-14 §2 #7 与 §5 路径对齐实现（/v1/algorithms*）。验证：459 单测（API +3：空态/有数据/OpenAPI）、PG 集成 API 2 passed（含新端点真库）、ruff/mypy 通过；栈内实测三端点返回正确（qfq_close_v1 active）、headless 渲染 /algorithms 页面（注册表 + 两空态 + 导航选中，无控制台报错）。
<!-- SECTION:NOTES:END -->
