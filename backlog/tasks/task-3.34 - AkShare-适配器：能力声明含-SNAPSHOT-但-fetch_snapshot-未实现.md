---
id: TASK-3.34
title: AkShare 适配器：能力声明含 SNAPSHOT 但 fetch_snapshot 未实现
status: To Do
assignee: []
created_date: '2026-09-27 07:41'
updated_date: '2026-09-27 15:16'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
priority: low
ordinal: 71000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
AkShare 适配器能力表声明 SNAPSHOT，但未实现 fetch_snapshot（调用抛 NotImplementedError），hub.get_snapshot 在 AkShare 路由下会失败；对外文档复核时发现并记入已知问题。期望：能力声明与实现一致——实现 fetch_snapshot（按适配器契约与字段映射）并从文档移除「已知问题」，或从能力声明移除 SNAPSHOT 并明确路由行为。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 能力声明与实现一致（实现 fetch_snapshot 或移除 SNAPSHOT 声明，二选一）
- [ ] #2 回归测试：对应路由下的 get_snapshot 行为有明确断言（成功或结构化 UnsupportedCapability）
- [ ] #3 文档同步（docs/data-sources.md 已知问题）与全量测试通过
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（工作区未提交，feat/akshare-snapshot 分支；经评审选择「实现 fetch_snapshot」路线）：① 适配器：capabilities 增 SNAPSHOT；fetch_snapshot 按资产类型路由全市场 spot 接口（股票 stock_zh_a_spot_em / ETF fund_etf_spot_em / LOF fund_lof_spot_em），一次调用覆盖全市场并**按请求代码本地过滤**（裸 6 位代码无法反推 venue，由请求映射回 canonical；未知代码自动剔除）；指数暂不支持（结构化 UnsupportedCapability，提示支持范围）；基金 spot 列名差异（开盘价/最高价/最低价）在适配器内统一为股票列名；date = Asia/Shanghai 当日（naive 零点）。② spec：akshare.toml 新增 [response.snapshot]（成交量 手→股 ×100；required 含代码/最新价/成交量/成交额）；tests/test_specs.py 的 COVERAGE 增列 akshare snapshot。③ capabilities.py 新增 (AKSHARE, SNAPSHOT) 端点能力（全市场接口 → max_codes_per_call=None）。④ 测试：tests/test_akshare_adapter.py +4（映射/过滤/单位/每端点单次调用、指数拒绝、空响应与缺列显式报错、hub.get_snapshot 端到端 + 能力表断言），原 test_capabilities 更新为四能力。⑤ 文档：docs/data-sources.md §3.2（能力与已知问题：指数暂不支持、全市场接口本地过滤、单位换算）、docs/components.md（能力表快照列与限制说明）。说明：卡面原描述（「声明 SNAPSHOT 但未实现」）在当前代码不成立（此前从未声明）；本次按「实现」路线落地并消除文档已知问题。验证：全量单测 + ruff + mypy(124 文件) 全绿。

优先级定位（用户确认，2026-09-27）：快照类边缘能力任务非高优先级——实现与文档已落地，但排期上不阻塞主线（数据质量 / REST / SDK 等）。待补：真实数据抽查（股票/ETF/LOF 各 1 例，核对列名与成交量单位）——本机网络不可达（宿主代理拒连、直连被重置），可在联网环境补验。
<!-- SECTION:NOTES:END -->
