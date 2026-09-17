---
id: TASK-3.27
title: 因子依赖图：注册期构图 + 调度门控 + 上游指纹
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-17 14:52'
updated_date: '2026-09-17 18:21'
labels: []
milestone: m-0
dependencies:
  - TASK-3.12
parent_task_id: TASK-3
ordinal: 66000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
派生引擎要显式描述指标之间的依赖关系（MA20 / HV20 / ADX …）：inputs 统一引用 dataset.field，field 可以是 raw 字段或其它因子输出（dataset.derived.output）；由此在注册期构图（无环/输入存在性校验）、运行期自动生成 job 依赖门控、审计期记录上游算法指纹以检测混合 vintage。三根元数据支柱之一（数据/身份/依赖），质量由一致性涌现。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 注册期构图：inputs 引用解析（raw 字段 vs 因子输出 dataset.derived.output）；无环校验 + 输入存在性 + 拓扑序（CI 与运行时一致）
- [ ] #2 调度门控：因子任务的 job_dependencies 由输入自动生成（复用 Runtime Dependency Manager），上游成功后才入队
- [ ] #3 物化按拓扑序（含子图展开）；循环/缺失上游在注册期即拒绝（错误含具体引用路径）
- [ ] #4 审计：因子投影记录上游算法指纹（依赖集合的 algorithm_id 哈希）；上游升级未重算 → 读时判定不一致（异常/过期标记）
- [ ] #5 文档（doc-10 §3.6 三根支柱、doc-11 inputs 语法）+ 全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. derived/graph.py：FactorGraph（从字典+注册表构图）——节点=因子(dataset.output)；边：输入为 raw 字段（访问面）或因子输出（dataset.derived.output，跨数据集亦可）；校验无环、因子引用可解析、拓扑序（稳定排序）
2. consistency.py 接入：check_consistency 汇总图错误（循环含具体路径、因子输入未声明输出、下游 latest 依赖未物化上游等约束）
3. tasks.py：derive 任务 job_dependencies 自动生成（因子输入 → 上游 derive job_id；两遍注册收集 job_id 后回填依赖）
4. 指纹：上游算法指纹 = 依赖集合（传递闭包）的 algorithm_id 排序哈希；materialize 时写入投影列 upstream_fingerprint 并在 MaterializeReport 暴露；materialize 前置校验：latest 下游的因子输入必须有已物化且指纹一致的上游投影（否则报可执行错误）
5. 测试：构图/环/缺失引用/拓扑序；任务依赖门控（上游成功前下游不入队）；指纹写入与不一致报错；PG 集成复核
6. 文档：doc-10 §3.6 因子图落地口径、doc-11 §4（因子输出引用语法）
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（未提交）：① derived/graph.py：FactorGraph.from_dictionary（节点=dataset.output；输入按 物理字段=数据依赖 / derived.output=因子依赖 解析；输出与字段同名报错）；validate（引用可解析、latest 下游要求上游 latest、无环）；cycles/topological_order/upstream_closure/fingerprint（传递上游 algorithm_id 集合哈希）。② consistency.check_consistency 汇总图错误。③ 字典 CI：因子输出引用合法化、输出与物理字段同名拒绝、因子输出禁 @mode 后缀。④ tasks：两遍注册，下游 dependencies=上游 derive job_id（门控实测：上游未成功下游 dependency_blocked）；derive scope 改为空（依赖按 job_id+scope+window 匹配，因子身份由 job_id 承载）。⑤ engine：upstream_fingerprint()、物化前置 _check_upstreams（比对上游投影算法列+上游指纹，不一致抛 UpstreamStale）、投影新增 upstream_fingerprint 审计列、MaterializeReport 暴露。⑥ inputs：因子输入读上游 latest 投影（as_of 对齐，早于 computed_at 抛 AsOfNotAligned）。⑦ 测试 tests/test_platform_factor_graph.py 8 项（构图/环/约束/缺失引用/依赖门控/指纹陈旧/对齐）。⑧ 文档 doc-10 §3.5、doc-11 §4、docs/sdk.md（upstream_stale）。验证：485 单测 + PG 集成 6 passed + ruff/mypy 通过。范围说明：AC#3『子图展开』的按需链式计算随 TASK-3.25（本任务交付 latest 链的物化前置校验、拓扑序与调度门控）。

复审修复（7 项+小问题，未提交）：① 高：链式因子挂 cron 永不物化（Dispatcher 对被拦 intent 只计数不保留）→ WorkerPool 在父任务成功后以同窗口重投递满足全部依赖的直接子任务（幂等由 job_key 保证；Runtime 端到端语义更新：parent 成功一轮执行 parent+child，手动重投递为 duplicate）；② 中：因子输入忽略 entity_ids/window → _read_factor_projection 增 _filter_frame（实体/事件窗口过滤，与数据输入同语义）；③ 中低：空结果物化 IndexError（影子表已换名却报失败）→ 指纹取局部变量，报告不再依赖首行；空投影语义统一（读返回空表、meta 返回 None 三元组并跳过校验）；④ 中低：因子输入 + inline_sql 产出无效 SQL → 一致性校验拒绝（读模型内联仅支持物理字段）；⑤ 低：宽泛 except 文案区分（投影不可读：未物化/旧版本缺审计列）；⑥ 低：同数据集 output 重名静默覆盖 → 构图期报错；⑦ 低：依赖门控不含 version_dimension（正确性由物化前置指纹校验兜底）——记录待后续。小问题：validate 去无用参数、拓扑入度忽略未登记引用、公开 read_factor_projection_meta、去重复导入。测试：+5 项（过滤/空结果/inline 拒绝/重复输出/链路重投递），Runtime 端到端断言更新。验证：489 单测 + PG 集成 9 passed + ruff/mypy 通过。
<!-- SECTION:NOTES:END -->
