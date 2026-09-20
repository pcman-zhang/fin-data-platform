---
id: TASK-3.25
title: 访问面：Factor API（严格 as_of 对齐 + 结构化异常）
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-17 14:34'
updated_date: '2026-09-20 05:02'
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
- [ ] #3 **依赖引用解析**：输入为 raw 字段（经访问面）或其它因子输出（子图求值，拓扑序 + 单请求 memoize）；跨层引用不可混淆
- [ ] #4 读路径不写库（测试断言零写入）；pin algorithm_id 复现路径可用
- [ ] #5 文档同步与全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 引擎拆分：DerivedEngine.compute(output, inputs, ...)（结果校验 + 元数据），execute = read_inputs + compute（原语义不变）
2. Factor API（derived/factor_api.py）：FactorMeta/FactorResult/FactorAPI.read() 两态——materialize=latest 读投影（as_of 对齐 + 窗口覆盖校验 + 实体/窗口过滤 + 审计元数据）；materialize=none 直接计算
3. 子图求值：按需链（因子输入）由 FactorGraph 拓扑序递归计算 + 单请求 memo；上游已物化（latest）时优先读投影（对齐校验），否则递归计算；读路径零写入
4. 结构化异常：复用/补齐 factor_not_materialized / as_of_not_aligned / upstream_stale / window_not_covered（含可执行 hint）
5. 测试：两态读取、按需链子图、混合链（上游物化）、对齐/覆盖异常、零写入断言
6. 文档：doc-21 §3 两态与子图口径、docs/sdk.md 对齐
<!-- SECTION:PLAN:END -->
