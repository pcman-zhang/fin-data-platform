---
id: TASK-3.25
title: 访问面：Factor API（严格 as_of 对齐 + 结构化异常）
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-17 14:34'
updated_date: '2026-09-20 05:32'
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
- [x] #1 Factor API 两态：物化投影读取（返回 algorithm_id / data_generation / computed_at）与按需计算（materialize=none，不落库）
- [x] #2 严格对齐：as_of < computed_at / 窗口未覆盖 / 未物化 → 结构化异常（含可执行 hint）；输入滞后校验（inputs_stale）随 TASK-3.26 输入水位查询一并落地
- [x] #3 依赖引用解析：输入为 raw 字段（经访问面）或其它因子输出（子图求值，拓扑序 + 单请求 memoize）；跨层引用不可混淆
- [x] #4 读路径不写库（测试断言零写入）；pin algorithm_id 复现路径可用
- [x] #5 文档同步与全量测试/ruff/mypy 通过
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（未提交）：① 引擎拆分 compute(output, inputs, ...)（结果校验 + 元数据），execute = read_inputs + compute（原语义不变）；② derived/factor_api.py：FactorAPI.read() 两态——materialize=latest 读投影（as_of 对齐、窗口覆盖（window_not_covered）、实体/窗口过滤、返回 algorithm_id/data_generation/computed_at/upstream_fingerprint；pin 与投影算法不一致报错）；materialize=none 走子图求值；catalog() 因子清单（含上游指纹）；FactorMeta/FactorResult/FactorSummary 导出；③ 子图求值 _evaluate：拓扑序递归 + 单请求 memo；上游 latest 优先读投影（对齐校验），否则递归计算；target 支持 pin（algorithm_id）；④ errors 增 WindowNotCovered（code=window_not_covered，docs/sdk.md 已列）；inputs.filter_frame 转公开供投影过滤复用；⑤ 测试 tests/test_platform_factor_api.py 6 项（按需链菱形 memo（计数=1）、混合链优先物化上游、pin/未知算法、物化元数据+对齐+覆盖+过滤、读路径零写入（mart 表集合与代次不变）、catalog 指纹）；⑥ 文档 doc-21 §3（两态/子图/读不写库/覆盖异常）。验证：495 单测 + PG 集成 9 passed + ruff/mypy 通过。

复审修复（5 项，未提交）：① 中：read() 未把 algorithm_id 转发给物化读取 → pin 静默忽略 → 已转发并补回归测试（投影算法与 pin 不一致报错；空投影无法校验时回落字典 active 并注明）；② 低中：窗口覆盖校验语义与文档不符 → 统一为**表级**覆盖校验（先校验后过滤），docs/sdk.md 与 doc-21 措辞改为「表级覆盖，空档由结果体现」；③ 低：上游 latest 未物化时改为**回落递归计算**（doc-21「优先读投影，否则递归」），补回归测试；④ 低：消除 factor_api 与 inputs 的投影读取重复 → inputs.read_projection_frame()（统一审计列/对齐/覆盖/过滤）+ ProjectionAudit，_read_factor_projection 变薄封装；⑤ 低：entities 类型统一 Sequence[int]。验证：497 单测（+2 回归）+ PG 集成 9 passed + ruff/mypy 通过。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
落地 Factor API：DerivedEngine.compute 拆分（execute 复用，语义不变）；FactorAPI.read 两态——物化投影读取（as_of 严格对齐、表级窗口覆盖、实体/窗口过滤、审计元数据、pin 不一致报错）与按需计算（子图求值：拓扑序递归 + 单请求 memo，上游 latest 未物化回落递归）；inputs.read_projection_frame 统一投影读取（审计/对齐/覆盖/过滤）；读路径零写入（mart 表集合与代次断言不变）；catalog() 清单含上游指纹。验证：497 单测（factor API 8 项）+ PG 集成 9 passed + ruff/mypy 通过；复审 5 项修复随附。范围：inputs_stale（读取时输入水位校验）划入 TASK-3.26。
<!-- SECTION:FINAL_SUMMARY:END -->
