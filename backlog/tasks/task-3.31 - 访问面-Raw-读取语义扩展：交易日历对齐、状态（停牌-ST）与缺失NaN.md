---
id: TASK-3.31
title: 访问面 Raw 读取语义扩展：交易日历对齐、状态（停牌/ST）与缺失=NaN
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-20 08:23'
updated_date: '2026-09-27 08:34'
labels: []
milestone: m-0
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

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 read(..., align_calendar=True)：交易日历对齐产「交易日 × 标的」预期行（日历 = 字典声明的 expected_dates.calendar，同一 as_of PIT 读取）；非交易日无行；缺行数值 null（pandas NaN）不填充
- [x] #2 状态列：status（ok/suspended/missing）+ is_suspended/is_st（约定 {domain}.daily_status，同一 as_of 最新版本；无状态行为 null）；盘中停牌（有 bar）以 bar 为准
- [x] #3 结构化异常：不支持形态/日历缺条目/保留列冲突 → unsupported_alignment；缺 window/entities 或窗口非法 → invalid_alignment_scope；日历在 as_of 不可见 → alignment_calendar_unavailable（含窗口与覆盖提示）
- [x] #4 读路径零写入；align 缺省关闭时 read/read_sql/读模型/派生引擎行为完全不变（回归）
- [x] #5 测试覆盖：三态/非交易日/日历与状态 PIT（as_of 与版本去重）/复权组合/异常/保留列冲突/零写入 + 全量测试/ruff/mypy 通过
- [x] #6 文档同步：doc-11 §3.8（新增，含交易所并集与保留列约定）、doc-21 §2/§3/§4、docs/sdk.md（示例/语义/错误码）、docs/components.md §7 与 README；因子「无 bar 不输出」标注过渡口径（本次不改实现）
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. access/errors.py：新增结构化异常 unsupported_alignment / invalid_alignment_scope / alignment_calendar_unavailable（AccessError 子类）
2. access/reader.py：read() 增 align_calendar（缺省 False，行为不变）；对齐路径——按字典 coverage.expected_dates.calendar 同一 as_of 单表 PIT 读日历取交易日；网格 = 交易日 × entities；行情（含复权组合）与状态（约定 {domain}.daily_status，存在则同一 as_of 读最新版本）在 pandas 内合并；派生 status 列（ok/suspended/missing，盘中停牌以 bar 为准）；ReadMeta 增 aligned/calendar_dataset/status_dataset/trading_days
3. access/__init__.py：导出新异常与相关常量
4. tests/test_platform_access_align.py：三态/非交易日/日历与状态 PIT（as_of 与版本去重）/复权组合/异常/零写入/缺省不变
5. 文档：doc-11 §3.7 与 doc-21 §3（经 backlog CLI）；docs/sdk.md（示例/错误码）；docs/components.md §7；README 口径说明；因子「无 bar 不输出」标注为过渡口径（本次不改实现）
6. 全量 pytest + ruff + mypy；任务卡记录
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（工作区未提交，feat/access-status-semantics 分支）：① access/errors.py 新增 UnsupportedAlignment / InvalidAlignmentScope / AlignmentCalendarUnavailable（结构化 code/hint）；② access/reader.py：read() 增 align_calendar（缺省 False，read_sql 与既有路径零改动）——_alignment_plan 校验（显式 window/entities、业务键 = 实体 × 事件时间、日历条目与状态数据集存在且同键）→ 日历按同一 as_of PIT 读（dataset_asof_sql 单表，is_open）→ 网格（交易日 × 标的，int64/object 显式 dtype）→ 行情（含复权组合）与状态（约定 {domain}.daily_status，同一 as_of 最新版本）pandas 合并 → status（ok/suspended/missing，盘中停牌以 bar 为准）+ is_suspended/is_st（nullable boolean，无状态行为 null）；ReadMeta 增 aligned/calendar_dataset/status_dataset/trading_days，状态数据集缺省时附 warning；③ STATUS_* 词汇收敛到 access 并从 source/read.py 复用（单一实现）；④ 测试 tests/test_platform_access_align.py 11 项（三态/非交易日无行/多标网格排序/日历 PIT 与不可见报错/状态 PIT 与版本/状态缺省双态/复权组合/scope 异常/不支持形态/空 entities/零写入与缺省不变）；⑤ 文档：docs/sdk.md（示例/语义行/错误码/边界含因子过渡口径）、docs/components.md §7、README 访问面条目；doc-11 §3.8 与 doc-21 §2/§3/§4 经 backlog CLI 更新。验证：全量单测 + ruff + mypy(116 文件) 全绿。

评审修复（3 项，随实现提交）：① [中] 对齐保留列守卫——数据集字段与 status / is_suspended / is_st 重名时抛 unsupported_alignment（修复前：daily_status 自身对齐会被状态合并后缀化并 KeyError，同类字段会被静默覆盖产生重复列）；② [低] 日历判定 v1 为窗口内任一交易所开市日并集——代码注释与 docs/sdk.md、doc-11 §3.8 标注该限制；③ [低] alignment_calendar_unavailable 消息带出请求窗口、hint 带出覆盖起点（区分「超出覆盖范围」与「PIT 不可见」）。测试新增保留列冲突 1 项并强化日历不可见断言。验证：全量单测 + ruff + mypy(116 文件) 全绿；align 专项 12 项。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
访问面 Raw 读取语义扩展落地：read() 增 align_calendar（缺省关闭，read_sql 内联 / 读模型 / 派生引擎行为不变）——按字典 coverage.expected_dates.calendar 与按域约定状态数据集 {domain}.daily_status 补齐「交易日 × 标的」预期行，输出 status（ok/suspended/missing，盘中停牌以 bar 为准）与 is_suspended/is_st（nullable，无状态行为 null）；缺行数值 null（pandas NaN），不填充；日历与状态同一 as_of 严格 PIT；结构化异常 unsupported_alignment / invalid_alignment_scope / alignment_calendar_unavailable（含窗口与覆盖起点提示）；日历 v1 取任一交易所开市日并集；数据集字段与保留列（status / is_suspended / is_st）冲突显式报错不覆盖数据。验证：tests/test_platform_access_align.py 12 项 + 全量单测 + ruff + mypy(116 文件) 全绿（合并后 main 复跑）；文档 docs/sdk.md / docs/components.md §7 / README 与 doc-11 §3.8 / doc-21 §2/§3/§4。
<!-- SECTION:FINAL_SUMMARY:END -->
