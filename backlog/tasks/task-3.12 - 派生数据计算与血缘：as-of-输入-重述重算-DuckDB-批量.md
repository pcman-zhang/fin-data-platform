---
id: TASK-3.12
title: 派生数据计算与血缘：as-of 输入 / 重述重算 / DuckDB 批量
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-13 08:50'
updated_date: '2026-09-17 13:42'
labels: []
milestone: m-0
dependencies:
  - TASK-3.2
  - TASK-3.3
  - TASK-3.18
parent_task_id: TASK-3
ordinal: 33000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
按 doc-10 §3.5 / doc-11 §4 落地 DerivedEngine：算法注册（@register + meta.algorithm_registry 生成，CI 与字典一致）+ 定义解析（字典 derived，含 materialize/refresh）+ 计划器/执行器（读模型内联字段级 / DuckDB 批量 / 按需计算）；as_of 输入 + algorithm_id（默认 active，可 pin 复现）；物化策略 none|latest（latest 单份投影、可重建、升级全量重算+代次切换）；升级事件（effective_from/reason）；不落多版本派生数据。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 DerivedEngine 骨架：@register 注册表 + meta.algorithm_registry 生成 + 字典 derived（materialize/refresh）解析与 CI 一致
- [x] #2 as_of 输入 + algorithm_id（默认 active / 可 pin）执行；响应元数据（algorithm_id / inputs as_of / data_generation）
- [x] #3 物化策略 none|latest：latest 投影单份可重建、升级重算+代次切换；不落多版本派生数据；测试覆盖
- [x] #4 升级事件记录与查询；文档同步（doc-10/11/13）；全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
切片 A：算法注册与契约（AC#1）
1. 新增 `derived` 包：registry.py（`@register(id, version, owner, effective_from?, reason?)` + AlgorithmSpec + 校验：id 命名/唯一、implementation=函数真实模块路径、docstring 含 Formula/PIT、inputs 合法）+ price.py（qfq_close_v1 参考实现）
2. 字典模型扩展：DerivedEntry 增 materialize(none|latest，默认 none)/refresh(on_demand|scheduled，默认 on_demand)；JSON Schema 重导出；daily_bar.yaml 显式登记
3. CI 一致性：字典 derived ↔ 代码注册表双向校验（字典每条须在注册表；注册表未被引用者标 deprecated，历史 id 不删除）+ 测试
4. 迁移 0004：meta.algorithm_registry / meta.algorithm_events / meta.data_generation；同步入口（幂等 upsert）

切片 B：as-of 执行与算法契约（AC#2）
5. 输入读取：inputs 的 dataset.field → canonical 表 + as-of 过滤（knowledge_time <= as_of；SCD2/区间按 pit_class），转 Arrow
6. DuckDB 运行器（纯计算引擎：Arrow 注入，不用 postgres 扩展）；算法契约 run(inputs, *, as_of) -> pa.Table（输出含 business_key + output）
7. DerivedEngine.execute(output, as_of, *, algorithm_id=None, entities/window)：默认 active、可 pin；响应元数据 algorithm_id / inputs_as_of / data_generation；前视防护测试（as_of 后知识时间不可见）

切片 C：物化 latest（AC#3）
8. 规划器：按字典选择服务形态（none → inline/on_demand；latest → 批量物化）
9. 最新投影：单份表 + 建新代次 + 原子 swap（PG）/事务替换；写 meta.data_generation；升级 → 全量重算 + 代次切换；不落多版本
10. 内联（读模型字段级）：生成 as-of SQL 表达式（qfq_close），语义测试验证；不新建 mart 读模型表（属 TASK-3.10 边界）

切片 D：升级台账 + Runtime 挂载 + 文档（AC#4）
11. 升级事件记录（@register 元数据 + CLI）与查询函数
12. Runtime：派生任务注册（version_provider=algorithm_id；executor → DerivedEngine；refresh=scheduled 挂窗口），并入 build_sync_runtime
13. 文档同步：doc-11 §4 / doc-13 §1·§7 / doc-10 §3.5
14. 全量 pytest + ruff + mypy（+ 集成如适用）
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
复权因子推导（事件→累计因子）属派生管线；需与 Tushare adj_factor / Fuyao 预计算复权价对账（doc-2 §6.16）。

算法登记方案（2026-09-13，doc-11 §4）：派生指标代码实现 + @register(id, version)，字典登记 output/algorithm_id/implementation/owner/inputs/description；算法升级=新 algorithm_id（历史永久保留）；派生结果记录 algorithm_id 以审计回溯；CI 校验 id 唯一/实现可导入/docstring 含 Formula+PIT/inputs 存在。

设计定稿（2026-09-13）：存输入与算法，不存多版本派生结果；algorithm_id 升级=新 id、旧实现永久保留（复现靠 pin+重算）；三种服务形态（读模型内联/按需计算/最新投影）；物化=可重建缓存（Cache Never Owns Data 延伸）；meta.algorithm_registry + algorithm_events。

依赖更新（2026-09-14）：按 doc-20 增加前置 TASK-3.18（FinDataRuntime 骨架）；派生执行作为 Runtime 的 Derived Engine Executor 角色挂载，版本维度用 algorithm_id。

切片 A/B 完成（未提交）：derived 包（registry @register + AlgorithmSpec、consistency 三方一致性、price.qfq_close_v1 参考实现、schema/store/sync/CLI）；字典 DerivedEntry 增 materialize/refresh（schema JSON 已重导出、daily_bar.yaml 显式登记）；迁移 0004（meta.algorithm_registry / algorithm_events / data_generation，漂移测试随附）；storage/schema.py 合并派生控制面表；pyproject 增 derive extra（duckdb+pyarrow）与 mypy 覆盖。引擎：inputs.read_inputs（as-of 过滤 + 最高 version 去重 + 窗口/实体过滤，IN 展开绑定）、engine.DerivedEngine.execute（pin/default active、结果列校验、data_generation 元数据）、projection_name。验证：mypy 105 文件通过、ruff 通过、434+ 单测通过；一次性 PG（timescaledb）实测 0001→0004 迁移/幂等/降级重升 + CLI --check/--sync/--list 落库正确。踩坑：duckdb 1.5 用 to_arrow_table（.arrow() 返回 Reader、fetch_arrow_table 已废弃）。

切片 C/D 完成（未提交）：planner（DerivedPlan：service=on_demand|materialize、projection、inline_available）、inline_sql（算法 SQL 模板 + as-of CTE 渲染，跨 SQLite/PG/DuckDB 纯字符串字面量）、materialize（影子表 __next + 事务内 DROP/RENAME 原子换名 + meta.data_generation + algorithm_id/as_of/computed_at/data_generation 审计列；pin 历史 algorithm_id 复现）、generation_stamp（YYYYMMDDTHHMMSSZ，doc-12）；tasks.register_derived_tasks（仅 latest 注册 derive 任务、version_provider=algorithm_id、refresh=scheduled 挂 FDP_DERIVE_SCHEDULE、成功按域失效缓存）；runtime/__main__ 装配派生任务（fail fast），bootstrap 接受外部 registry；配置：Dockerfile/.env.example/docker-compose 增 derive extra 与 FDP_DERIVE_SCHEDULE；文档同步 doc-10 §3.5 实现落地、doc-11 §4 实现说明、doc-13 §1.2 派生控制面表。验证：30 derived 单测 + 全量 450 passed、ruff/mypy 通过；PG 集成（test_integration_derived：as-of 回读/内联 SQL/物化原子换名与代次）2 passed。

切片 D 收尾（未提交）：Runtime 启动装配顺序=就绪检查 → 派生任务注册 → 算法登记同步（幂等）→ 启动；装配两条日志（派生任务装配 / 算法登记同步）；doc-17（自动生成）重生成（含 3 张 meta 表 + 修复 TASK-3.20 遗留的 provider 类型漂移）；report.py 补 3 表说明与测试断言。验证：全量 pytest 450 passed/23 deselected、ruff check 通过、mypy 106 文件通过；PG 集成 2 passed（as-of 日期回读 / 内联 SQL 在 PG / 物化原子换名+代次+审计列）；栈内验证：迁移 0004 已应用（schema_revision 通过）、runtime 启动日志「派生任务装配：无（无 latest 物化派生）」「算法登记同步：total=1 active=1 deprecated=0 events=0」、容器内 CLI --list 输出正确。

复审修复（5 项，未提交）：① 高：scheduled 派生任务静默失效（_run_spec 只走 due_provider，derive 无水位/起点）→ TaskSpec 增 window_provider，app._run_spec 优先使用；派生任务窗口=触发日（避免 window=None 被 create_run 以 job_key 去重，同日重复触发同键幂等、次日为新运行）；② 中：退役算法 upsert 清空 dataset/output/inputs → inputs 空写 NULL + update COALESCE（SQL）/内存实现 merge，归属信息保留；③ 中低：物化成功后缓存失效未接线 → __main__ 统一 cache_from_env() 同时注入 sync 与派生任务；④ 低：materialize 的 as_of 未按 normalize_as_of 归一 → 修正；⑤ 低：to_sql 增 chunksize=1000。复审其余结论（as-of 纪律、迁移漂移、注入面、先校验后写入）无问题。验证：456 passed（derived 36）、ruff/mypy 通过、PG 集成 3 passed（新增退役 COALESCE 用例）、栈内 runtime 重启装配日志与 healthz 正常。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
落地派生引擎（as-of 输入 / 算法登记 / 最新投影 / Runtime 挂载）：`derived` 包（@register 注册表 + 字典↔注册表↔实现三方一致性 + qfq_close_v1 参考实现 + store/sync/CLI）、迁移 0004（meta.algorithm_registry / algorithm_events / data_generation）、字典 materialize/refresh、DerivedEngine（execute 默认 active/可 pin、inline_sql as-of CTE、materialize 单份投影 + 原子换名 + 代次）、Runtime derive 任务挂载（version_dimension=algorithm_id、触发日窗口、按域失效缓存）。验证：456 单测（derived 36）+ PG 集成 3 passed + ruff/mypy 通过；栈内迁移 0004 已应用、Runtime 装配日志（派生任务装配 / 算法登记同步）与 /healthz 正常；doc-10 §3.5、doc-11 §4、doc-13 §1.2 同步，doc-17 重生成。复审 5 项（scheduled 静默失效、退役清空归属、缓存失效未接线、as_of 未归一、to_sql 未分块）已修复并补测试。
<!-- SECTION:FINAL_SUMMARY:END -->
