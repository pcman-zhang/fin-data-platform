---
id: TASK-3.33
title: 同步引擎：窗口含交易日但 0 行成功仍推进水位（应判软失败）
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-27 07:41'
updated_date: '2026-09-27 09:27'
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
- [x] #1 窗口含交易日（落库日历可见）且源端 0 行（fetched==0）且该实体在窗口末（含）前已有数据（在市）→ 软失败 EmptySourceWindow（不推进水位、走既有重试/退避）；窗口无交易日 / 日历不可见 / 实体无历史数据（前上市、首次同步）→ 允许 0 行成功
- [x] #2 运行记录与错误信息可区分「源空响应」（EmptySourceWindow）与「通道异常」（SourceError 等），失败原因可观测
- [x] #3 测试覆盖：空响应软失败与重试/耗尽、幂等重放（fetched>0、rows_written=0）不误判、部分缺失不误判、无交易日成功、日历不可见 fail-open、前上市窗口允许
- [x] #4 文档同步（配置手册同步任务语义与已知局限）与全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. ingestion/common.py：EmptySourceWindow（软失败异常）+ calendar_open_days（落库日历窗口内交易日；无日历行 → None 无法判定）+ entity_has_history（在市判定：窗口前是否存在该实体数据，按字典事件时间列）+ 日历查询原语（daily_status 复用，单一实现）
2. ingestion/tasks.py _sync_executor：fetched==0 且日历可见且有交易日 且 实体窗口前已有数据 → raise EmptySourceWindow（消息含数据集/代码/窗口/交易日数/原因提示）；其余情形维持成功
3. Runtime 不改：失败走既有 retry/退避、on_success 不执行（水位不动）；错误入 job_runs.error 与健康输出
4. 测试 tests/test_platform_sync_guard.py：原语（日历可见/无行 None、在市判定）；执行器与 Runtime 端到端（软失败+重试、耗尽 DEAD、水位不动、无交易日成功、fail-open、前上市允许、重放/部分缺失不误判）
5. 文档：docs/configuration.md §6.2 同步任务语义与局限（退市后/全窗停牌会持续软失败）；必要时 troubleshooting 补查
6. 全量 pytest + ruff + mypy；任务卡记录
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（工作区未提交，fix/sync-empty-window 分支）：① ingestion/common.py 新增 EmptySourceWindow（软失败异常）与共享原语 trading_days / calendar_open_days（窗口无日历行 → None，无法判定）/ entity_has_history（在市判定：窗口末（含）之前是否已有数据，按字典事件时间列）；daily_status 改用共享日历原语（消除重复实现）。② ingestion/tasks.py _sync_executor 增 _guard_empty_window：fetched==0 且日历可见且有交易日且实体在市 → EmptySourceWindow（消息含数据集/代码/窗口/交易日数；与通道异常 SourceError 可区分）；其余情形照常成功。③ 触发条件取 fetched（源端 0 行）而非 rows_written——幂等重放（fetched>0、written=0）与部分缺失不误判。④ Runtime 不改：失败走既有重试/退避，on_success 不执行（水位不动）。⑤ 测试 tests/test_platform_sync_guard.py 9 项（有历史软失败 + 水位不动、重试耗尽 DEAD、无历史允许、无交易日允许、日历不可见 fail-open、复权因子同路径、守卫矩阵（重放/部分缺失不误判）、原语、水位表直查）；原 ingestion/status/bootstrap 20 项回归通过。⑥ 文档：docs/configuration.md §6.2（语义与已知局限）与 docs/troubleshooting.md §4/§5。验证：全量单测 + ruff + mypy(116 文件) 全绿。

评审修复（3 项）：① [低] _guard_empty_window docstring 与实现对齐——在市判定语义为「窗口末（含）之前已有数据」（覆盖窗口内已有部分数据的情形），清除原文「窗口前」的歧义；② [低] _dataset_table 事件时间字段缺失时改为显式 ValueError（原 next(...) 会抛 StopIteration），并说明仅在市判定路径可达；③ [很低] 删除与首用例水位断言重复的 test_watermark_unchanged_after_soft_failure_direct_sql（减少维护面）。验证：全量单测 + ruff + mypy(116 文件) 全绿；sync guard 专项 8 项。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
同步引擎空窗口守卫落地：源端 0 行（fetched==0）且窗口含交易日且实体在市（窗口末含之前已有数据）时判软失败（EmptySourceWindow）——Runtime 走既有重试/退避、on_success 不执行（水位不动），错误入 meta.job_runs 且与通道异常 SourceError 可区分；窗口无交易日、日历不可见（fail-open）或实体无历史（前上市/首次同步）允许 0 行成功。判定基于 fetched 而非 rows_written（幂等重放与部分缺失不误判）。共享原语 calendar_open_days / entity_has_history / trading_days（daily_status 复用，消除重复实现）。验证：tests/test_platform_sync_guard.py 8 项 + 全量单测 + ruff + mypy(116 文件) 全绿（合并后 main 复跑）；文档 docs/configuration.md §6.2、docs/troubleshooting.md §4/§5；已知局限（退市后、全窗口停牌）已文档化。
<!-- SECTION:FINAL_SUMMARY:END -->
