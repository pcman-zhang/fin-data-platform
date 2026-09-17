---
id: TASK-3.24
title: 访问面：Raw 规范化读取 API（PIT + 复权口径组合）
status: To Do
assignee: []
created_date: '2026-09-17 14:34'
updated_date: '2026-09-17 14:34'
labels: []
milestone: m-0
dependencies:
  - TASK-3.3
  - TASK-3.12
parent_task_id: TASK-3
ordinal: 63000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
在 Canonical 与消费方之间落地访问层：统一的 Raw 读取 API（PIT + 口径组合），派生引擎与消费出口共用同一实现；复权组合只实现一次（口径与数据源 Router 一致）。契约见 doc-21《SDK 接口面与契约》、doc-10 §3.5 口径归属。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 访问面 API 落地（fin_data_platform/access）：read(dataset, fields, *, as_of, adjust=None|qfq|hfq|raw, entities, window) -> Arrow + 元数据
- [ ] #2 字典增机器可读 adjust 声明（modes / factor_dataset / default）+ CI 校验；daily_bar 默认 qfq、指数类 none
- [ ] #3 derived/inputs.py 改为经访问面读取（算法不再自拼复权）；测试覆盖 qfq/hfq/raw 口径、as_of 锚点、不支持组合报错
- [ ] #4 文档同步（doc-10/11/13 + docs 公开页）与全量测试/ruff/mypy 通过
<!-- AC:END -->
