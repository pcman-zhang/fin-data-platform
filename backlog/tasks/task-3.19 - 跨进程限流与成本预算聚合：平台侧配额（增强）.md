---
id: TASK-3.19
title: 跨进程限流与成本预算聚合：平台侧配额（增强）
status: In Progress
assignee: []
created_date: '2026-09-14 14:09'
updated_date: '2026-10-07 13:52'
labels: []
milestone: m-0
dependencies:
  - TASK-3.9
parent_task_id: TASK-3
ordinal: 57000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
背景：Hub 的令牌桶限流与预算统计为**进程内**生效；Runtime 支持 scheduler / worker 拆分进程（doc-20 §3.2）后，多进程总量不受控，付费源存在超预算风险。本任务作为增强项，将限额与预算聚合到平台侧共享层。

目标：
1. 基于 TASK-3.9（Redis CacheBackend）实现跨进程共享令牌 / 计数：多进程（runtime-scheduler / runtime-worker / 手工触发）下按源总量不超配；
2. 预算与告警按源聚合可查询（不再依赖单进程 hub.stats）；
3. 缓存不可用时 **fail-open**：回退进程内限额，采集不被阻塞（doc-10 §3.4 缓存非权威）；
4. 权威数据仍在 PostgreSQL；共享计数属可重建的运行时状态。

参考：doc-10 §3.4（缓存非权威 / fail-open）、doc-20 §4.5（并发与限流）、TASK-3.9。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 跨进程共享限流 / 计数（基于 TASK-3.9 CacheBackend）：多进程并发下按源总量不超配
- [ ] #2 预算与告警按源聚合可查询（多进程口径，不依赖单进程 stats）
- [ ] #3 缓存不可用时 fail-open：回退进程内限额且采集不阻塞；缓存恢复后自动接管
- [ ] #4 一致性测试（多客户端并发计数不超限）+ 文档同步；pytest/ruff/mypy 通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
<!-- SECTION:PLAN:BEGIN -->
评审通过（2026-10-07），分支 feat/task-3.19-quota；决策：固定窗口计数（原子 INCR + TTL）实现共享限流；含 WebUI「配额与成本」小卡。
1. quota/ 新模块：shared（SharedRateLimiter：窗口内共享计数、超限等待/超时、fail-open 回退进程内）；usage（预算回调 → 共享计数 calls/cost_micro/alerts；聚合读取 read_usage）；config（FDP_RATE_LIMITS / FDP_BUDGET_CALLS / FDP_BUDGET_COST / FDP_BUDGET_WARN_RATIO 解析）。
2. 缓存层：CacheBackend 增 incrby（InMemory/Redis 原子）+ LayeredCache.try_incr/try_incrby/read_counter（L2 优先、异常 None=fail-open）。
3. Hub 装配缝：HubConfig.limiter_factory；facade._limiter_for 使用工厂（缺省行为不变）。
4. 平台装配（build_hub）：按环境构建 rate_limits/budget + 注入共享限流工厂与预算回调（on_record/on_alert → 共享计数 + 日志告警）。
5. API：GET /v1/usage（按源：今日 calls/cost、预算与剩余、告警、限流配置、共享缓存可用性）；ApiContext 增 cache（build_context 装配）。
6. WebUI：总览页「配额与成本」小卡。
7. 测试：多客户端共享缓存并发一致性（不超配）、fail-open 回退与恢复、预算回调聚合、配置解析、hub 工厂缝、API 端点。
8. 文档：configuration（新环境变量 / /v1/usage）、doc-10 §3.4 / doc-20 §4.5 注记、README。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
评审修复（2026-10-07）：H1 生产入口 __main__ 未透传缓存 → build_hub(..., cache=active_cache)（真实部署已验证共享计数写入）；H2 Redis 计数 TTL 自愈（INCRBY 后检查 TTL，缺失即补设，防永久锁死）；M1 窗口改为 capacity/rate（持续速率 ≈ rate，含 sub-1 rate 与 burst 语义）；M2 告警口径改为共享计数派生（alerts，多进程一致）+ fired 触发计数（审计）；M3 补测试：窗口推进（对齐时钟）、故障冷却短路、L2 故障不落 L1、Redis TTL 自愈、hub 装配透传；L：配置有限性/源名校验、基础回调链式调用、tokens 边界、compose 配额变量透传、文档同步。
<!-- SECTION:NOTES:END -->

<!-- SECTION:PLAN:END -->
