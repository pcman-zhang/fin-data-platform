---
id: TASK-3.33
title: 同步引擎：窗口含交易日但 0 行成功仍推进水位（应判软失败）
status: To Do
assignee: []
created_date: '2026-09-27 07:41'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
priority: high
ordinal: 70000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
源端返回空表（如出网被切断、SDK 吞异常返回空）时，同步任务仍记 succeeded 并推进水位、窗口被跳过，形成永久缺口（TASK-3.29 实测时发现并记录）。期望：窗口含交易日（按 ref.trade_calendar 判定）且本次 0 行写入时判软失败（可重试、不推进水位），并在运行记录与告警中可见；窗口确无交易日时才允许 0 行成功。范围：ingestion 同步判定与 Runtime 水位推进。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 窗口含交易日且 0 行写入 → 软失败（不推进水位、可重试）；窗口无交易日 → 允许成功
- [ ] #2 运行记录与错误信息可区分「源空响应」与「通道异常」，失败原因可观测
- [ ] #3 测试覆盖：空响应重试、部分缺失不误判、无交易日成功
- [ ] #4 文档同步与全量测试/ruff/mypy 通过
<!-- AC:END -->
