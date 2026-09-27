---
id: TASK-3.30
title: 第二个因子：ADX（Wilder 平滑 / 多输入 / 端到端验证）
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-20 08:19'
updated_date: '2026-09-27 07:45'
labels: []
dependencies: []
parent_task_id: TASK-3
priority: medium
ordinal: 70000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
在字典登记 daily_bar.adx（algorithm_id=adx, version=1），输入 high/low/close 显式 @hfq；实现经典 Wilder ADX(14)：TR/+DM/-DM → 平滑（SMA 种子 + Wilder 递推）→ +DI/-DI → DX → ADX。验证：与独立 pandas 实现数值对账、预热不足不输出、as-of 防前视、物化 → FactorAPI 读取；更新字典 CI 用例（输出集合含 adx）与任务卡。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 字典登记 daily_bar.adx（algorithm_id=adx version=1，inputs=[high@hfq, low@hfq, close@hfq]，materialize=latest / on_demand）
- [x] #2 实现与独立复算一致：Wilder 平滑（SMA 种子）与 numpy 显式循环逐点对账
- [x] #3 预热不足 2×14−1 根 bar 严格不输出；as-of 防前视（测试覆盖）
- [x] #4 端到端：物化 → Factor API 读取；/v1/algorithms 含 adx v1
- [x] #5 共享平滑原语（smoothing.py，不登记为因子）；全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现（feat/ma20-factor 分支，未提交）：① derived/factors.py 新增 adx（ADX(14)，DuckDB 计算 TR/+DM/-DM，Python 按实体流式 Wilder 递推）；② 平滑定位经讨论定为**共享原语不登记因子**——新增 derived/smoothing.py（Wilder 累加器 + wilder_smooth，种子='前 n 期均值'，wilder=EMA α=1/n 的递推同构仅种子不同），ADX 复用；③ 字典登记 daily_bar.adx（algorithm_id=adx version=1，inputs=[high@hfq, low@hfq, close@hfq]，materialize=latest/on_demand）；④ 缺失语义：预热不足 2×14-1 根 bar 严格不输出（与 ma20 一致）；停牌/ST 与真缺失的区分经讨论确认属访问面职责，已立 **TASK-3.31**（交易日历对齐/状态列/缺失=NaN）；⑤ 测试 tests/test_platform_factor_adx.py（5 项：原语教科书递推、字典一致性、独立 numpy 显式循环复算+预热边界、as-of 防前视、物化→FactorAPI 读取）；同步更新 test_platform_derived/test_platform_dictionary 的 shipped 输出集合与任务注册断言。验证：全量单测 + ruff + mypy(110 文件) 全绿；真实数据（600519.SH 79 bar）物化 53 行、独立复算最大差 0、范围 14.21~39.59；镜像重建后 /v1/algorithms 含 adx v1（3 个显式输入）。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
落地第二个因子 ADX(14)：derived/factors.py 新增 adx（DuckDB 计算 TR/+DM/-DM，Python 按实体流式 Wilder 递推）；共享平滑原语 derived/smoothing.py（wilder_smooth，SMA 种子）不登记为因子；字典登记 daily_bar.adx（algorithm_id=adx version=1，inputs=[high@hfq, low@hfq, close@hfq]，materialize=latest / on_demand）；预热不足 2×14−1 根 bar 严格不输出。验证：tests/test_platform_factor_adx.py 5 项（教科书写法递推/字典一致性/独立 numpy 逐点复算+预热边界/防前视/物化+FactorAPI）+ 全量测试 + ruff/mypy 通过；真实数据 600519.SH 79 bar 物化 53 行、独立复算最大差 0、范围 14.21~39.59；/v1/algorithms 含 adx v1（见实施笔记）。
<!-- SECTION:FINAL_SUMMARY:END -->
