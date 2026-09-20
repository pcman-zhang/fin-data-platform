---
id: TASK-3.29
title: 复权因子采集通道：adj_factor 同步 / 任务注册 / 水位
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-20 05:56'
updated_date: '2026-09-20 06:47'
labels: []
milestone: m-0
dependencies: []
parent_task_id: TASK-3
ordinal: 68000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
验证 TASK-3.28 时发现：ingestion 侧只有日线同步（sync_daily_bar），复权因子无采集写入路径（栈内 adj_factor 长期为 0），而访问面默认口径已改为 hfq，导致派生因子在真实环境会全部为 NULL。需新增 adj_factor 同步（hub.get_adjust_factors）、canonical 幂等写入、Runtime 任务注册与水位推进，并与日线同步同批调度（因子先于派生）。
<!-- SECTION:DESCRIPTION:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 研究 sync_daily_bar 结构（实体解析 / 幂等写入 / 修订版本 / 水位）与 hub.get_adjust_factors 输出契约
2. 新增 ingestion/adj_factor.py：sync_adjust_factor（canonical 幂等写入 + 值变化追加修订版本 + SyncResult）
3. Runtime 装配：build_sync_runtime 为每个 code 同时注册日线 + 复权因子任务（同窗口/调度/水位；成功后按域失效缓存）
4. 测试：幂等 / 值变化修订 / 水位推进 / 任务注册与装配（FakeHub 增因子接口）
5. 栈内实测：容器内经代理同步 adj_factor → 行数核对 → MA20 重物化复核
6. 文档：配置手册同步任务说明（日线+因子）
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现与验证（未提交）：① 新增 ingestion/adj_factor.py（sync_adjust_factor：entity 解析 → hub.get_adjust_factors → canonical 幂等写入 + 值变化追加修订版本 + 无 publish_time 列）、ingestion/common.py（SyncResult/provider 识别/知识时间共享原语，daily_bar 同步去重）；② Runtime 装配：register_adj_factor_task（job_id=sync.cn_equity.adj_factor.<code>，与日线同窗口/调度/水位；共享 _sync_executor/_watermark_handler），bootstrap 为每个 code 同时注册日线与因子任务；③ 测试：新增 2 项（幂等 + 修订版本 + 任务级跑通与水位推进），FakeHub 增因子接口；bootstrap 用例改为双任务断言；④ 容器内真实验证：经宿主代理（host.docker.internal:7897）tushare daily/adj_factor/trade_cal 三接口全部正常；sync_adjust_factor 拉取 **79 fetched / 79 written**，栈内 adj_factor 79 行（2026-06-01~09-18，8.4464~8.6463，与宿主侧一致）。验证：499 单测 + ruff/mypy 通过。⑤ 发现两处待办：容器出网代理修复（compose 行）原在 3.28 改动中，本分支已同样落地；**0 行成功仍会推进水位**（若日历显示窗口含交易日，应判软失败/重试），建议后续在同步引擎补上——已记入本卡。

代码审查修复（同一分支）：① 因子任务按能力门控——bootstrap 新增 _supports_adjust_factors（读 adapter.capabilities，FakeHub 无 registry 时保持默认注册），源未声明 ADJUST_FACTORS（如 akshare）时跳过因子任务并告警；新增用例 test_build_sync_runtime_skips_factor_task_without_capability；真实适配器探针 tushare=True / akshare=False。② 文档补齐：.env.example 增 FDP_CONTAINER_PROXY/NO_PROXY 模板；docs/configuration.md 增「容器出网代理」小节，并在 §6.2 说明每个 code 同时注册日线+因子任务、无能力源跳过、因子 18:00 发布的时间约束。复验：499 单测 + ruff + mypy(src 108 文件) 全绿。
<!-- SECTION:NOTES:END -->
