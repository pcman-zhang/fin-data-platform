---
id: TASK-3.32
title: 源层读取升级：交易日历与标的每日状态落库（禁 SQL join / 预填充发布 / 状态感知读取）
status: In Progress
assignee: []
created_date: '2026-09-20 08:35'
updated_date: '2026-09-20 13:34'
labels: []
dependencies: []
parent_task_id: TASK-3
priority: high
ordinal: 69000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
范围：源层（fin_data_hub + 采集/落库与源侧读取），**本任务不含平台访问面**（见 TASK-3.31）。

硬约束：
1. 所有跨表组合必须在 pandas 程序内 join，**SQL 不得 JOIN**（读取与计算路径）；
2. 交易日历**落库**，作为**预填充数据**随 docker 发布（不依赖运行时源调用；避免 trade_cal 空响应/限流导致调度空转）；
3. 标的**每日历史状态**入库（entity_id, trade_date, 状态标志）；
4. 读取 API 必须考虑交易日历与当日标的状态：按“交易日 × 标的”产出预期行并标注状态，区分三态——停牌（无交易）/ 非交易日（无行）/ 真缺失（NaN，数据质量问题）。

实现要点（侦察结论）：
- 字典新增 ref.trade_calendar（is_open/pretrade_date 等，业务键含日历/交易所标识）与 cn_equity.daily_status（停牌/ST/上市状态；业务键 entity_id+trade_date）；DDL 由字典生成，随迁移落地（当前最新 0004）；
- 预填充：日历种子（如 CSV/Parquet）打进包/镜像，启动或迁移时幂等导入（ON CONFLICT DO NOTHING）；覆盖范围与刷新机制待定；
- 状态采集：hub 增加停牌能力（tushare suspend_d；适配器已声明 TRADE_CALENDAR/MARKET_EVENTS），ST 状态由 namechange 区间推导，采集任务写 daily_status（append-only + 修订语义）；
- 源侧读取：pandas 内 join 日历 × 状态 × 行情，输出状态列；停牌行值可空但保留行；交易日缺行=真缺失（NaN）；
- 测试：日历导入幂等、状态推导、三态区分、禁 JOIN 的约束检查（如 SQL 静态扫描）；文档同步。
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
动工前决策（已确认）：① 日历发布=包内种子（CSV：exchange, date, is_open, pretrade_date；SSE/SZSE，2015–2030），启动/迁移幂等导入（ON CONFLICT DO NOTHING），后续可用 tushare trade_cal 刷新并做限流保护；② 状态首期=停牌 + ST（含 *ST），来源 tushare（suspend_d + namechange 区间推导），上市/退市/涨跌停后续扩展；③ 读取入口=hub 保持纯直连取数，新增平台包内源侧读取模块（复用 hub 取数 + 纯 pandas join 日历×状态×行情，遵守禁 SQL JOIN 约束），本任务不含平台访问面。

code review（分支级 main...HEAD）结论 NO-GO，修复清单（已确认走 0005 修订路线）：
1. [H1 迁移] storage/migrations.py 新增 reference_data_statements()（脚手架参照 algorithm_meta_statements() 374-438 行；建表/索引复用 baseline_statements() 的字典遍历，压缩复用 compression_statements(order_by_override)），写 migrations/versions/0005_reference_data.py（revision=0005_reference_data, down_revision=0004_algorithm_meta）；同时冻结 0003 使用的数据集集合（新增字典条目不得改变 0003 生成结果）；更新 tests/test_platform_migrations.py 的 head 断言（0004→0005）；修后跑全量 pytest（当前 2 failed 于 26/96 行）。
2. [H2 接线] docker-compose migrate 在 upgrade 后调用 seed（幂等）；读取侧仍走 HubTradeCalendar 的切换属步骤②，但需在本卡标注。
3. [M1 语义] 刷新/修订通道：seed 明确为一次性首灌（更新 trade_calendar.yaml 描述与 CLI 文案），修订走后续版本追加通道。
4. [M2 校验] is_open 白名单 {0,1} 显式报错；批次内业务键去重；空种子/缺行按显式错误处理。
5. [L3] 提交未跟踪文件（storage/__main__.py、tests/test_platform_seed.py）与工作区改动；任务卡推进 In Progress。
已验证通过：wheel 含 data/*.csv（干净 venv 实读）；与 tushare SSE 逐日对拍 is_open/pretrade_date 0 差异；无 SQL JOIN/无凭证/离线测试；ruff+mypy 绿。
<!-- SECTION:NOTES:END -->
