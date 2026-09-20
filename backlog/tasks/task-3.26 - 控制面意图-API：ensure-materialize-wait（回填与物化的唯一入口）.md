---
id: TASK-3.26
title: 控制面意图 API：ensure / materialize + wait（回填与物化的唯一入口）
status: To Do
assignee: []
created_date: '2026-09-17 14:34'
updated_date: '2026-09-20 05:32'
labels: []
milestone: m-0
dependencies:
  - TASK-3.12
  - TASK-3.21
parent_task_id: TASK-3
ordinal: 65000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
把回填/物化做成控制面意图：SDK/REST 提交幂等任务（job_runs 审计，Runtime 执行），附 run 句柄与 wait；客户端永不持写权限。契约见 doc-21 §1/§2。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 ensure / materialize 意图接口（SDK 库接口 + REST）：幂等提交、返回 run 句柄、wait(timeout)
- [ ] #2 权限边界：客户端无写权限，仅提交意图；任务状态可在平台侧审计
- [ ] #3 读取路径的输入滞后校验：窗口超出输入水位时抛 inputs_stale（含可执行提示，承接 TASK-3.25 AC#2）
- [ ] #4 测试覆盖：幂等（重复提交命中既有运行）、超时语义、失败可见、inputs_stale
- [ ] #5 文档同步与全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
范围补充（TASK-3.25 收尾时确认）：承接「读取路径输入滞后校验」——窗口超出输入数据集水位时抛结构化异常 inputs_stale（含触发同步/回填的可执行提示），与 Factor API 的 as_of/覆盖校验配套。
<!-- SECTION:NOTES:END -->
