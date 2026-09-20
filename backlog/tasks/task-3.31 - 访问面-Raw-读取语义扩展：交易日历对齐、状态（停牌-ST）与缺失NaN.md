---
id: TASK-3.31
title: 访问面 Raw 读取语义扩展：交易日历对齐、状态（停牌/ST）与缺失=NaN
status: To Do
assignee: []
created_date: '2026-09-20 08:23'
labels: []
dependencies: []
parent_task_id: TASK-3
priority: high
ordinal: 71000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
问题：read() 目前只返回数据行，无法区分三类“无数据”：

1. **停牌**：当日无 bar，是“无交易”而非缺失；不应被消费方当作数据质量问题；
2. **状态属性（ST/风险警示等）**：是标的属性，不是缺失；应以标志列/结构化元数据附带，而非让数值为空；
3. **真缺失**：数据质量问题，按业内惯例以 NaN 表达（不做隐式填充）。

需求（待设计）：① 可选交易日历对齐——区分“应有但无”（停牌/缺失）与“无需有”（非交易日）；② 状态列（停牌/ST/涨跌停等）来源与表达（标志列或元数据；来源候选：tushare suspend_d / ST 列表 / namechange）；③ 缺失=NaN，不隐式填充；填充策略（none/ffill/interpolate）仅由消费侧显式选择；④ 契约与文档更新：doc-11 §3.7（访问面语义）、doc-21（读取契约）、docs/sdk.md/README 口径说明；⑤ 派生因子消费语义联动（当前 ma20/adx 对无 bar 行严格不输出，属过渡口径）。

依赖：TASK-3.24（访问面 Raw 规范化读取，已合并）+ TASK-3.29（复权因子通道，已合并）。
<!-- SECTION:DESCRIPTION:END -->
