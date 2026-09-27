---
id: TASK-3.34
title: AkShare 适配器：能力声明含 SNAPSHOT 但 fetch_snapshot 未实现
status: To Do
assignee: []
created_date: '2026-09-27 07:41'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
priority: medium
ordinal: 71000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
AkShare 适配器能力表声明 SNAPSHOT，但未实现 fetch_snapshot（调用抛 NotImplementedError），hub.get_snapshot 在 AkShare 路由下会失败；对外文档复核时发现并记入已知问题。期望：能力声明与实现一致——实现 fetch_snapshot（按适配器契约与字段映射）并从文档移除「已知问题」，或从能力声明移除 SNAPSHOT 并明确路由行为。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 能力声明与实现一致（实现 fetch_snapshot 或移除 SNAPSHOT 声明，二选一）
- [ ] #2 回归测试：对应路由下的 get_snapshot 行为有明确断言（成功或结构化 UnsupportedCapability）
- [ ] #3 文档同步（docs/data-sources.md 已知问题）与全量测试通过
<!-- AC:END -->
