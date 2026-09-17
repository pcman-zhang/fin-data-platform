---
id: TASK-3.27
title: 因子依赖图：注册期构图 + 调度门控 + 上游指纹
status: To Do
assignee: []
created_date: '2026-09-17 14:52'
updated_date: '2026-09-17 17:49'
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
