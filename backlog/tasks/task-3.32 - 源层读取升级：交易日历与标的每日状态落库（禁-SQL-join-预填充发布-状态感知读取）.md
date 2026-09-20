---
id: TASK-3.32
title: 源层读取升级：交易日历与标的每日状态落库（禁 SQL join / 预填充发布 / 状态感知读取）
status: In Progress
assignee: []
created_date: '2026-09-20 08:35'
updated_date: '2026-09-20 14:01'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. [H1] migrations.py：修订数据集台账 + 冻结 0001/0003 数据集集合；新增 reference_data_statements()（复用 schema_sql/timescale_statements，限定 0005 数据集）；生成 migrations/versions/0005_reference_data.py
2. [H1] 更新 tests/test_platform_migrations.py（head 0005、0005 漂移/覆盖、冻结集合下的基线断言）
3. [H2] storage CLI 增 --migrate；docker-compose migrate 执行 --migrate --seed（幂等）
4. [M1] seed 语义澄清：trade_calendar.yaml 描述、CLI/seed.py 文案
5. [M2] seed 校验：is_open 白名单 / 批次内业务键去重 / 空种子与缺行显式报错 + 单测
6. 全量 pytest + ruff + mypy，栈内/迁移演练验证
<!-- SECTION:PLAN:END -->

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

审查修复完成（工作区未提交，fix/ref-migration 分支）：
[H1] 迁移：storage/migrations.py 增「修订数据集台账」REVISION_DATASETS（0005→ref.market/ref.trade_calendar）与 frozen_datasets()（= 字典全集 − 已登记增量；未登记的新字典条目会立即触发 0001/0003 漂移失败）；baseline_statements 与 0003 的压缩/解压语句改为只作用于冻结集（新增 compression_statements(datasets=...) / _domain_schemas() 冻结）；build_metadata 支持 datasets 过滤（过滤模式不引入范围外手写 ref 表）；_dictionary_statements() 为各修订共用生成器；新增 migrations/versions/0005_reference_data.py（建表/索引/hypertable/压缩+策略；down 仅删自身两表）；head 断言更新为 0005。

[H1] 测试：更新 6 项（冻结集下的基线/降级/索引断言、head=0005），新增 4 项（0005 漂移/覆盖与降级、冻结集契约、字典全覆盖）。验证：511 单测 + ruff + mypy 全绿；栈内临时库实测：0005 upgrade → ref.trade_calendar hypertable（compression enabled，segmentby=exchange_id，orderby=trade_date,knowledge_time,version）→ seed 2/9496 → 重跑 0/0 → downgrade 0004 表清理干净 → 再 upgrade+seed 正常；CLI --migrate --seed 端到端一致（临时库已销毁）。

[H2] 接线：storage CLI 增 --migrate；docker-compose migrate 命令改为「python -m fin_data_platform.storage --migrate --seed」；docs/configuration.md 服务表同步。读取侧切换（HubTradeCalendar/状态感知）仍属步骤②，不在本轮。

[M1] 语义：trade_calendar.yaml 描述、seed.py 与 CLI 文案明确「一次性首灌、非修订通道，后续修订走版本追加」。 [M2] 校验：种子缺列/缺值/非法日期/is_open∉{0,1}/批次内业务键重复/空种子均显式 ValueError；新增 5 项单测（含 CLI 无动作返回 2）。

代码审查修复（review 后）：① [中] storage CLI 的 --migrate 与 --seed 改为共用同一 StorageConfig（含 FDP_DATABASE_HOST 覆盖）并显式传 DSN 给 upgrade()——原先 seed 走 from_env() 不读 FDP_DATABASE_HOST，两动作可能指向不同库；已用「DATABASE_HOST=invalid.example + FDP_DATABASE_HOST=127.0.0.1」临时库实测两动作同库、正常落种子。② 修订台账增加 REVISION_STATEMENTS（修订→DDL 生成器），覆盖校验测试改为按台账枚举，新增台账一致性用例（512 单测 + ruff + mypy 全绿）。③ write_reference_data_revision 文档补「0005 已执行后须新增修订」告警。④ 注意：migrations/versions/0005_reference_data.py 目前未跟踪，提交时需 git add。
<!-- SECTION:NOTES:END -->
