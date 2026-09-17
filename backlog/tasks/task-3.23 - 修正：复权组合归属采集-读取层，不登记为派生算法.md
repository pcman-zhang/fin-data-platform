---
id: TASK-3.23
title: 修正：复权组合归属采集/读取层，不登记为派生算法
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-17 14:01'
updated_date: '2026-09-17 14:11'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
ordinal: 62000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
评审修正：qfq（raw × f/f_anchor）属 FinDataHub Router 采集/读取层（doc-5）的组合能力，不应经 Derived Engine 输出（doc-10 §3.5）。需从字典 derived 移除、删除错放的参考实现、测试改为测试内自证算法，并清理误登记的 meta 行。
<!-- SECTION:DESCRIPTION:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 字典：移除 cn_equity.daily_bar 的 derived 块（qfq 属 Router 采集/读取层组合，doc-5）
2. 删除错放的参考实现 src/fin_data_platform/derived/price.py；清理 derived/__init__ 文档引用
3. 测试改造：测试内自证算法（adjusted_close_v1，含 inline_sql）替代生产参考实现；字典/引擎/物化/任务用例全部改用测试注册表；字典测试断言 shipped 字典不再登记 qfq
4. 文档修正：doc-10 §3.5 / doc-11 §4 明确复权组合归属（采集/读取层），移除 qfq_close_v1 参考实现表述
5. 存量清理：删除栈库误登记行（meta.algorithm_registry），重启 runtime 复核空注册表；WebUI 页面复验空态
6. 验证：全量单测 + PG 集成 + ruff/mypy
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
修正完成（未提交）：① 字典移除 cn_equity.daily_bar 的 derived 块（qfq 属采集/读取层组合，doc-5）；② 删除错放的参考实现 derived/price.py；③ 测试改造为测试内自证算法（adjusted_close_v1，含 inline_sql）——测试字典注入 synthetic derived 条目 + 测试注册表，引擎/物化/任务用例全部显式注入 registry（register_derived_tasks 增 registry_algorithms 参数）；字典测试改为「shipped 字典不登记任何派生输出」+ 通用可追溯校验；④ 文档：doc-10 §3.5（三种形态示例改因子字段；新增口径归属说明——复权归采集/读取层，派生不读未调整 OHLCV）、doc-11 §4（示例改 ma20 因子 + 口径归属注记）；⑤ 存量清理：删除栈库误登记行 qfq_close_v1，重建镜像后 runtime 同步 total=0，/v1/algorithms=[]，WebUI 算法页三处空态正常。验证：461 单测 + PG 集成 5 passed（derived 3 + API 2）+ ruff/mypy 通过。
<!-- SECTION:NOTES:END -->
