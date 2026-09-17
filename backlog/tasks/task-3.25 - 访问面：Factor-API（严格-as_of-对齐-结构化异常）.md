---
id: TASK-3.25
title: 访问面：Factor API（严格 as_of 对齐 + 结构化异常）
status: To Do
assignee: []
created_date: '2026-09-17 14:34'
updated_date: '2026-09-17 14:34'
labels: []
milestone: m-0
dependencies:
  - TASK-3.12
  - TASK-3.24
parent_task_id: TASK-3
ordinal: 64000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
因子读取 API：物化投影（单份、严格知识锚对齐）与按需计算（materialize=none）两态；不对齐抛结构化异常（as_of_not_aligned / window_not_covered / factor_not_materialized / inputs_stale），读路径永不写库。契约见 doc-21 §3/§4。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Factor API 两态：物化投影读取（返回 algorithm_id / data_generation / computed_at）与按需计算（materialize=none，不落库）
- [ ] #2 严格对齐：as_of < computed_at / 窗口未覆盖 / 未物化 / 输入滞后 → 结构化异常（含可执行 hint）
- [ ] #3 读路径不写库（测试断言零写入）；pin algorithm_id 复现路径可用
- [ ] #4 文档同步与全量测试/ruff/mypy 通过
<!-- AC:END -->
