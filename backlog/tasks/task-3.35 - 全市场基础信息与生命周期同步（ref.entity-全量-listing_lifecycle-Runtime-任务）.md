---
id: TASK-3.35
title: 全市场基础信息与生命周期同步（ref.entity 全量 / listing_lifecycle / Runtime 任务）
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-27 15:36'
updated_date: '2026-09-27 16:50'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
priority: high
ordinal: 72000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
问题：注册表当前仅有「采集按需注册」的标的（entity_type=equity、name 空），没有全市场登记表；cn_equity.listing_lifecycle（PIT Universe 权威来源）字典已登记但无采集通道，universe 推导无数据可依。目标：从 Tushare 基础信息构建全市场身份与生命周期并入库（仅 Runtime 任务，无 CLI）。

范围与口径（评审已定）：
- 身份登记四类：stock_list→equity、etf_list→etf、fund_list→fund、index_list→index（ref.entity / ref.entity_code_history，SCD2）；
- 生命周期分级：股票（stock_list + delist_list）→ listed [list_date, delist_date-1] + delisted [delist_date, null]；ETF/基金 → listed [list_date, null]（退市字段待实测后补）；指数仅登记身份、不写 lifecycle；
- knowledge_time 口径：首版 = 区间起点 start_date（稳定值，重跑幂等，历史 as-of 可还原）；修订版本 = 同步时刻；
- 修订语义：字段（status/end_date）变化追加版本（version+1），未变化不写；
- 停牌不入本表（per-day 停牌由 cn_equity.daily_status 负责）；
- 遗留清洗：名称空缺回填；entity_class 非法值（如遗留 stock）置空并计入统计 legacy_cleaned；
- 接线：Runtime 全局任务（dataset=cn_equity.listing_lifecycle；窗口 = 触发日；REFERENCE 能力门控；FDP_REGISTRY_SCHEDULE 可选，不配置=启动即首灌一次）。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 身份登记：四类清单（stock_list/etf_list/fund_list/index_list）全量写入 ref.entity（新增 + 名称/市场刷新；未变化不写；代码→entity_id 稳定）
- [x] #2 生命周期：股票两行规则（listed/delisted）；ETF/基金 listed 行；指数不写；knowledge_time 首版=start_date、修订=同步时刻；字段变化追加版本、幂等重跑零写入
- [x] #3 修订场景：退市日期变更 → listed 行 end_date 更新（新版本）+ delisted 行新增/更新
- [x] #4 遗留清洗：名称空缺回填；非法 entity_class 置空并计入统计 legacy_cleaned
- [x] #5 Runtime 接线：全局任务注册（REFERENCE 能力门控；触发日窗口；job_key 同日幂等）+ FDP_REGISTRY_SCHEDULE + 启动即首灌；端到端用例
- [x] #6 文档同步（docs/components.md、docs/configuration.md、doc-11 口径、docs/data-sources.md）与全量测试/ruff/mypy 通过
- [x] #7 管理界面可发起：GET /v1/jobs/defs（任务定义 + 派生 scope）+ POST /v1/jobs/trigger（仅 sync 类；触发日窗口；job_key/request_id 幂等；derive 类拒绝并提示用 materialize）；WebUI 任务页「全局任务」卡片可列出并触发（二次确认 + 结果反馈）
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. registry/store.py：EntityStore 增 refresh（SCD2：属性变化追加 version+1；未变化不写；名称/分类/市场回填；非法 entity_class 置空）
2. ingestion/market_registry.py：sync_market_registry(engine, hub, *, source=None) —— 身份四类清单 + 生命周期分级规则（knowledge_time 首版=start_date、修订=now）+ 统计（entities created/refreshed/unchanged/legacy_cleaned；lifecycle written/revisions/skipped）
3. ingestion/tasks.py：register_market_registry_task（全局 scope、window_provider=触发日）；runtime/__main__.py 装配（REFERENCE 能力门控 + FDP_REGISTRY_SCHEDULE）
4. 测试 tests/test_platform_market_registry.py：四类身份 / 股票两行 / ETF listed / 幂等重跑零写入 / 退市日期修订 / 遗留清洗 / 能力门控 / Runtime 端到端
5. 文档与字典：docs/components.md、docs/configuration.md、docs/data-sources.md；listing_lifecycle.yaml 描述补 knowledge_time 口径（不改 DDL）
6. 全量 pytest + ruff + mypy；任务卡记录

6. 通用触发链路：ControlClient.trigger（scope 由 job_id 约定推导；窗口=触发日）；REST /jobs/defs 与 /jobs/trigger
7. WebUI：Jobs 页新增「全局任务」卡片（列 scope="" 的 sync 任务 + 触发 + 确认 + 结果）
8. 测试：sync/身份/生命周期 + trigger（derive 拒绝、幂等、scope 推导）+ npm build
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（工作区未提交，feat/market-registry-sync 分支）：① registry/store.py：EntityStore 增 get_entity / update_entity（SCD2 属性刷新：变化时同 valid_from 追加 version+1、未变化不写；None=清值、哨兵=不改）。② ingestion/market_registry.py：sync_market_registry——身份四类 + 退市（stock/delist/etf/fund/index → ref.entity，含名称/市场回填与遗留 entity_class 清洗）+ 生命周期分级（股票两行 listed/delisted；ETF/基金 listed；指数仅身份；异常行跳过计数）；knowledge_time 首版 = 区间起点 15:00 CST 稳定值、修订 = 同步时刻；旧开放行被更正 → 零长度闭合（失效标记，append-only）；幂等（append_rows 物理键 + 无变化不写）。③ ingestion/tasks.py：register_market_registry_task（job_id=sync.reference.market_registry、scope=""、窗口=触发日）；runtime/keys.py 增 job_scope（约定推导）；control/client.py 增 trigger（仅全局 sync；按代码/derive 分别提示 ensure/materialize；窗口=触发日；幂等）。④ REST：GET /v1/jobs/defs（含派生 scope）、POST /v1/jobs/trigger（202；404/409/422）；GET /v1/entities/universe（as_of 必填、knowledge_as_of 可选严格 PIT、分页）——读面复用 registry.universe（EntityLookup 协议化 + RegistryReader 增 entity(as_of)/entity_many/lifecycle）。⑤ Runtime 装配：FDP_REGISTRY_SOURCE（缺省取 FDP_SYNC_SOURCE）+ FDP_REGISTRY_SCHEDULE（缺省空 = 启动即跑一次）+ REFERENCE 能力门控（supports_capability 公开化）。⑥ WebUI：Jobs 页新增「全局任务」卡片（defs 列表 + 最近运行状态 + 二次确认触发 + 错误 Alert）；dist 已重建。⑦ 测试：tests/test_platform_market_registry.py 9 项 + control +4 + API +2（defs/trigger/universe）；全量单测 + ruff + mypy(125 文件) 全绿；npm run build 通过。⑧ 文档：.env.example、docs/configuration.md §6.2、docs/components.md、docs/data-sources.md §3.1、docs/sdk.md；字典 listing_lifecycle.yaml（knowledge_time 口径）；doc-11 §3.9、doc-21 §1/§2/§3、doc-12 §2.2/§2.3/§2.4（经 backlog CLI）。待补：真实 Tushare 数据实测（五类清单全量拉取 + 登记/区间覆盖核对；本机网络不可达，联网环境补验）。

真实部署（docker compose 新镜像）发现并修复 2 项缺陷（随本轮提交）：① **source 未转发**——sync_market_registry 调 hub.get_reference(kind) 未传 source，Hub 要求显式来源（default_source=None）→ ValueError 被 except 吞掉 → 全部空帧「假成功」（rows_written=0）；已修为显式转发 source，并补 require_source 桩的回归测试。② **未调度全局任务不会启动首灌**——runtime 的未调度循环（ticker）只按水位窗口（due_provider），window_provider 型任务不会自动触发；已在 runtime 入口显式提交「今天窗口」首灌意图（同窗口由 job_key 幂等），并同步配置手册措辞。③ 旧「假成功」运行已按运维修正：meta.job_runs run_id=16 → interrupted（保留审计）。

收尾验证（2026-09-28，合并后 main 复跑）：① 全量 pytest 退出码 0、ruff 全绿、mypy 131 文件无问题；② WebUI 无头渲染实测：/jobs 页含「全局任务」卡片节点；③ 真实部署数据核对（docker compose 新镜像 + Tushare）：run 17 succeeded / rows_written=37,695 / 约 185 秒；ref.entity 29,650（fund 13,899 / index 8,000 / equity 5,910 / etf 1,840 / 历史 issuer 1）；cn_equity.listing_lifecycle 8,048 行 / 7,709 标的（listed 7,709 / delisted 339）；遗留清洗 000858.SZ v2 entity_class→NULL、名称回填 600519.SH v2→贵州茅台；退市样本 knowledge_time=退市日 07:00 UTC；PIT Universe as_of=2015-06-01 → 2,813（对照 2026-09-01 → 7,308；严格 PIT knowledge_as_of=2026-09-10 结果一致）。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
全市场身份登记与生命周期同步 + 通用触发链路（defs / trigger / WebUI 卡片）+ PIT Universe 读 API 均已交付并验证：全量单测 / ruff / mypy(131) 全绿（2026-09-28 复跑）；Docker Compose 新镜像真实部署（Tushare）run 17 = succeeded / 37,695 行，实体 29,650、生命周期 8,048 行（listed 7,709 / delisted 339），遗留清洗与名称回填实测通过，PIT Universe 2015-06-01 = 2,813；部署期修复 source 未转发与启动首灌缺失两项真实缺陷（含回归测试）。
<!-- SECTION:FINAL_SUMMARY:END -->
