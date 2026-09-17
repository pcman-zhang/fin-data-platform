---
id: TASK-3.11
title: FinDataPlatform SDK 发布与版本兼容（pip 包 / schema 矩阵 / 连接配置）
status: To Do
assignee: []
created_date: '2026-09-13 06:29'
updated_date: '2026-09-17 14:34'
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
发行物命名（2026-09-13）：SDK 包名 fin-data-platform，依赖 fin-data-hub（FinDataHub）。

SDK 接口模型采用 Pydantic v2；导出 JSON Schema 作为契约文档（doc-2 §6.17）。
<!-- SECTION:NOTES:END -->
