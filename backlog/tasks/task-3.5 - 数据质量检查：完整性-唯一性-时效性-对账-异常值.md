---
id: TASK-3.5
title: 数据质量检查：完整性 / 唯一性 / 时效性 / 对账 / 异常值
status: In Progress
assignee: []
created_date: '2026-09-13 06:00'
updated_date: '2026-09-27 19:14'
labels: []
milestone: m-0
dependencies:
  - TASK-3.3
parent_task_id: TASK-3
ordinal: 24000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
质量检查框架：完整性（缺失窗口/断点）、唯一性（重复键）、时效性（更新延迟）、跨源对账（复用 v0 对账框架）、异常值与跳变检测；每日质量报告与阈值告警。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 质量检查覆盖首批数据域并产出每日报告
- [ ] #2 关键指标跨源对账通过率 100%（阈值内）
- [ ] #3 异常检出可追溯到来源与时间窗口
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
评审通过（2026-09-28），分支 feat/task-3.5-quality：
1. 字典：新增 jump 规则种类（field/max_ratio/severity，doc-11 同步）；daily_bar/adj_factor/daily_status 的 coverage.universe_source 修正为 cn_equity.listing_lifecycle（在市口径，期望集合按配置范围受限、全市场作参考值）。
2. 新模块 quality/：① 规则执行器（unique/not_null/range/enum/expression，含受限表达式解析器）；② 完整性（coverage：期望实体×交易日 vs 实际，逐日覆盖率与断点）；③ 时效性（update_sla + 最新数据日 → lag）；④ 引用对账（reconcile against ref.*：共享键存在性；raw.* 未落地记 skipped）；⑤ 跨源对账（小样本 Tushare vs BaoStock：原始价一致 + 复权因子归一化，doc-8 SOP 子集，通过率入报告）；⑥ 跳变（jump）。
3. 存储：meta.quality_results（迁移 0007；append-only：run × dataset × check，含窗口/状态/严重级/违例数/样本/指标）。
4. Runtime：全局任务 quality.scan（kind=quality，scope=''）；FDP_QUALITY_*（SCHEDULE 可选 / LOOKBACK_DAYS=10 / DATASETS / CODES / RECONCILE_CODES）；触发链路扩展（POST /v1/jobs/trigger 支持 quality；WebUI 全局任务卡片纳入）。
5. API：GET /v1/quality/summary（日 × 数据集）+ GET /v1/quality/results（明细/过滤/分页）。
6. WebUI：新增「质量」页（日期 + 汇总表 + 明细 Drawer），dist 重建。
7. 测试：逐规则 + 覆盖率/时效/对账/跳变 + Runtime/API + 前端构建；文档同步（doc-11/doc-13、.env.example、docs/{components,configuration}.md）。
8. 验证：全量 pytest/ruff/mypy + docker compose 实测（真实数据出报告）。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实施进展（2026-09-28，分支 feat/task-3.5-quality）：① 字典：QualityRule 增 jump（field/max_ratio，正数校验）+ meta-schema 再生成；daily_bar / adj_factor / daily_status 的 coverage.universe_source → cn_equity.listing_lifecycle（在市口径，status=delisted 终态区间不计入）；daily_bar 声明 jump(close, max_ratio=5.0, warn)。② quality 模块：expr（受限表达式 AST→SQL）/ rules（7 类规则→违规行查询）/ runner（规则 + 完整性停牌感知 + 时效性）/ reconcile（跨源小样本 SOP 子集）/ schema+store（meta.quality_results）/ tasks（quality.scan 全局任务）。③ 存储：迁移 0007_quality_meta（由 quality/schema.py 渲染、台账登记）；storage/schema 合并。④ Runtime：FDP_QUALITY_* 装配（DATASETS/CODES/RECONCILE_CODES/LOOKBACK_DAYS/SCHEDULE）+ compose app-env 透传（顺带补 3.35 遗漏的 FDP_REGISTRY_* 两项）；触发链路扩到 quality。⑤ API：/v1/quality/summary + /v1/quality/results；WebUI「质量」页（日期/汇总/明细 Drawer）+ Jobs 全局任务卡纳入 quality。⑥ 测试：tests/test_platform_quality.py 7 项（规则逐类 / 完整性停牌感知 / 跨源通过·失败 / 任务端到端 / 触发 / API）+ 字典 jump 校验 + 迁移漂移；全量 pytest EXIT=0、ruff、mypy(141 文件) 全绿。⑦ 真实部署发现并修复：PG 类型不匹配——非实体键数据集（ref.trade_calendar，键 exchange_id）误用「停牌排除」子查询（daily_status.entity_id = text 键）；sqlite 夹具不报错、PG 直接 UndefinedFunction；已按「停牌排除仅适用于 entity_id 键」修正。旧运行 run 18 为 dead（终态不阻断同窗口重跑，无需运维修正）。

第三次部署复验（run 20，31 项检查，约 3 秒）：跨源对账真实通过（600519.SH：close 30 行重叠、最大差 0（容差 0.01）；复权因子最大差 0（容差 1e-4））；完整性 5/5 数据集覆盖率 100%（停牌感知在市口径）；时效性滞后 0；规则检查（unique/not_null/range/expression/jump）全过；引用对账修正后全过（共享键 = 业务键 ∩ 目标表；raw.tushare_daily 未落地 → skipped 占位）。报告口径：同日多次运行按最新一次聚合（旧 run 19 已置 interrupted，审计保留）。实测：POST /v1/jobs/trigger（幂等命中启动调度提交的运行）、GET /v1/quality/summary 与 /results、WebUI「质量」页无头渲染（含数据集行与覆盖率）。

代码评审（独立评审代理 + 自审，2026-09-28）发现并修复（均已补测试，共 16 项质量测试）：① 【高】完整性期望集合未按版本语义——静态 SCD2 期望（ref.entity）按键去重、区间期望取「获胜区间」（start_date/version/knowledge_time 最大者，与 registry.universe 一致）；修复前线上 listing_lifecycle 期望 20（重复版本），修复后 10。② 【中】/v1/quality/* 原走只读引擎读 meta（只读角色不授权 meta）→ 改走写连接（与任务/水位一致）。③ 【中】扫描终点收敛到「最近已收盘交易日」（16:30 CST，StoredTradeCalendar.last_closed；手工触发同样收敛），调度窗口同口径——避免盘中触发误判当日未发布数据。④ 【中】单条检查异常不再中断整轮（逐条 try/except → status=error 留痕）。⑤ 【中】值检查按业务键最新版本执行（latest_query；unique 仍查物理键）——被修正的历史值不判违规、jump 序列确定。⑥ 【低】停牌排除不再作用于区间型数据集；jump 显式浮点除法；freshness 规则显式提示未实现；跨源对账按共同末值归一化 + 单边缺失计入 dropped_rows；warnings 口径改为「失败/异常且 warn 级」；期望范围未应用时指标/消息提示。⑦ 文档：触发文案含 quality、components/configuration 质量口径、.env.example 补 FDP_QUALITY_DATASETS。验证：全量 pytest EXIT=0、ruff、mypy(141) 全绿。
<!-- SECTION:NOTES:END -->
