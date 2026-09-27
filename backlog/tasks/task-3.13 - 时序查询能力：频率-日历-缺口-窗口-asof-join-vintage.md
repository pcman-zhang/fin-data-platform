---
id: TASK-3.13
title: 时序查询能力：频率 / 日历 / 缺口 / 窗口 / asof join / vintage
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-13 08:56'
updated_date: '2026-09-27 12:56'
labels: []
milestone: m-0
dependencies:
  - TASK-3.1
  - TASK-3.3
parent_task_id: TASK-3
ordinal: 34000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
在数据平面/SDK 落地双时间轴时序能力：范围序列查询（多键×字段×频率）、日历/时区/会话、重采样与连续聚合、缺口策略（null/前值/最近值）、窗口与滚动（PIT 正确）、跨序列对齐与 asof join、vintage/版本历史查询、多频段（日频+分钟级）。存储侧用 TimescaleDB 连续聚合/time_bucket_gapfill/按频分级压缩保留支撑。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 时序查询面落地：get_series / get_cross_section / get_panel / get_versions（含 freq ∈ {1d,1w,1mo,1q,1y}、as_of 显式、fill ∈ {none,ffill}、calendar ∈ {trading,None}、adjust 参数）
- [x] #2 日历锚定与缺口策略规范明确并有测试：重采样锚点 = 该期最后一个交易日；缺行数值为 null 不隐式填充；ffill 仅使用 as_of 可见数据（停牌行亦可填充，status 列保留原因）
- [x] #3 重采样与基础窗口算子 PIT 正确：先按 as_of 过滤再聚合；rolling(window, op) 与 change(periods) 仅使用可见数据；不使用非 PIT 的数据库连续聚合（连续聚合留待非 PIT 读模型/性能优化）
- [x] #4 vintage 与版本历史可查：mode=history 返回全部可见版本（含 knowledge_time/version/ingest_time）；mode=vintage 取每个 event_time 的首个可见版本（as-first-reported）；分钟级多频段不在本期（高频透传为未来特性）
- [x] #5 跨序列对齐：asof_join（backward 默认，支持 by 分组与 tolerance）与宽表/长表（get_panel shape=wide|long）可用
- [x] #6 测试与文档：全量测试/ruff/mypy 通过；docs/sdk.md 与 doc-21（经 backlog CLI）同步
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. panel/（新包）：errors（unsupported_freq / invalid_fill 结构化异常）
2. panel/series.py：get_series（access PIT 读取 → 日历对齐（复用 3.31）→ 日历锚定重采样（open=first/high=max/low=min/close=last/volume·amount=sum/其它=last，可 agg 覆盖）→ fill（none/ffill）→ 基础窗口算子 rolling(window, op) / change(periods)，均 PIT 安全）；get_cross_section（单事件日截面，calendar=trading 时走对齐）；get_panel（长表/宽表）
3. panel/versions.py：get_versions（单表读取，不经访问面去重）：history = 全部可见版本；vintage = 每 event_time 首个可见版本；支持 as_of 截断
4. panel/join.py：asof_join（pandas merge_asof 语义：backward 默认 / by 分组 / tolerance）
5. 测试 tests/test_platform_panel.py：重采样锚定与聚合、fill/rolling/change 的 PIT 正确性（as_of 截断）、vintage 与版本历史、asof join、宽表/长表、异常（未知 freq/fill）
6. 文档：docs/sdk.md（时序查询节 + 语义要点）、doc-21 §2/§3（时序查询面与口径）
7. 全量 pytest + ruff + mypy；任务卡记录
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
范围说明：本期只做入库时序（双时间轴）；高频透传与延迟统计为未来特性（doc-2 §6.15，暂不开发）。

实现完成（工作区未提交，feat/timeseries-query 分支）：① 新包 panel/（errors + series + versions + join）——get_series（访问面 PIT 读取 → 对齐（复用 3.31）→ 日历锚定重采样 → fill → rolling/change）、get_cross_section（单事件日截面，calendar=trading 走对齐）、get_panel（长/宽表，(entity, field) 多级列）、get_versions（history 全部可见版本 / vintage 每事件日首个可见版本，as_of 可选截断）、asof_join（merge_asof 语义：backward 默认 / by 分组 / tolerance；事件时间列 date 进出）。② 口径：freq ∈ {1d,1w,1mo,1q,1y}（桶锚点 = 该期最后一个观测日；聚合默认 open=first/high=max/low=min/close=last/volume·amount=sum/其它=last，可 agg 覆盖）；fill ∈ {none,ffill}；rolling(window, op, min_periods) 与 change(periods) 在可见数据上按实体流式计算；状态列重采样：status = 任一 ok → ok，否则任一 suspended → suspended，否则 missing；is_suspended/is_st = any（全空 → NA）；不使用非 PIT 的数据库连续聚合。③ 结构化异常：unsupported_frequency / invalid_fill / invalid_argument；对齐路径需要显式 entities（沿用 access 校验）。④ 测试 tests/test_platform_panel.py 9 项（对齐三态/周频锚定与聚合/ffill 与 rolling/change PIT 安全/未来重述不参与聚合/截面/宽表长表/版本历史与 vintage/asof join 与 tolerance/异常矩阵）；⑤ 文档：docs/sdk.md（面总览 + 示例 + 语义行 + 错误码）、doc-21 §1/§3/§4（经 CLI）、doc-2 §6.14 实现落点说明（经 CLI）。验证：全量单测 + ruff + mypy(124 文件) 全绿。

评审修复（4 项）：① [中] asof_join 多实体分组失效——_prepare 原按 [by, on] 排序导致 merge_asof 抛 ValueError("left keys must be sorted")；改为按 [on, *by] 排序（by 分组由 pandas 内部处理），新增多实体 by 分组回归用例（输出按 by 分组排序 + 各实体向前取最近观测）；② [低] get_cross_section 参数还原为 date（去掉 date_ + **kwargs 兼容层；from __future__ import annotations 下注解安全）；③ [低] get_versions vintage 去重前在 pandas 内显式排序（消除对 SQL 排序的隐含依赖）；④ [低] 模块 docstring 标注 fill=ffill 在无预期行序列（calendar=None）为无操作。验证：panel 专项 10 项 + 全量单测 + ruff + mypy(124 文件) 全绿。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
时序查询面落地（新包 fin_data_platform.panel）：get_series（访问面 PIT 读取 → 日历对齐（复用 3.31 三态）→ 日历锚定重采样（桶锚点 = 该期最后一个观测日/交易日；默认 open=first/high=max/low=min/close=last/volume·amount=sum/其它=last，agg 可覆盖）→ fill ∈ {none,ffill} → 基础窗口算子 rolling(window, op, min_periods) 与 change(periods) 按实体流式计算）、get_cross_section（单事件日截面）、get_panel（wide=(entity_id, field) 多级列 / long）、get_versions（history 全部可见版本 / vintage 每事件日首个可见版本，as_of 可选截断）、asof_join（merge_asof 语义：backward 默认 / by 分组 / tolerance）。PIT 正确性：先按 as_of 过滤再对齐/重采样/填充/窗口，仅使用可见数据；不使用非 PIT 的数据库连续聚合（连续聚合留待非 PIT 读模型/性能优化，落点已写入 doc-2 §6.14）。结构化异常：unsupported_frequency / invalid_fill / invalid_argument；分钟级/高频透传不在本期（未来特性）。验证：tests/test_platform_panel.py 10 项 + 全量单测 + ruff + mypy(124 文件) 全绿（合并后 main 复跑）；文档 docs/sdk.md / doc-21 §1/§3/§4（CLI）/ doc-2 §6.14（CLI）。
<!-- SECTION:FINAL_SUMMARY:END -->
