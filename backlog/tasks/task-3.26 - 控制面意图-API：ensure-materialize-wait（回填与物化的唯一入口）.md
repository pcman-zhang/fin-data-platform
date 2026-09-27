---
id: TASK-3.26
title: 控制面意图 API：ensure / materialize + wait（回填与物化的唯一入口）
status: Done
assignee:
  - '@freeman'
created_date: '2026-09-17 14:34'
updated_date: '2026-09-27 11:27'
labels: []
milestone: m-0
dependencies:
  - TASK-3.12
  - TASK-3.21
parent_task_id: TASK-3
ordinal: 65000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
把回填/物化做成控制面意图：SDK/REST 提交幂等任务（job_runs 审计，Runtime 执行），附 run 句柄与 wait；客户端永不持写权限。契约见 doc-21 §1/§2。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 ensure / materialize 意图接口：库接口（fin_data_platform.control；SDK 薄封装归 TASK-3.11）+ REST（/jobs/sync 与 /jobs/materialize）：幂等提交（request_id / 同窗口 job_key 去重）、返回 run 句柄、wait(timeout) 超时抛错且任务继续
- [x] #2 权限边界：客户端无写权限，仅提交意图（只写 meta.job_runs）；任务状态可在平台侧审计（运行记录可查）
- [x] #3 读取路径的输入滞后校验：按需计算前校验数据输入的可见覆盖（knowledge_time <= as_of 的最晚事件时间），窗口超出抛 inputs_stale（含触发同步 / ensure 的可执行提示）
- [x] #4 测试覆盖：幂等（重复提交命中既有运行 / 多代码 request_id 拒绝）、超时语义、失败可见、inputs_stale、窗口越界
- [x] #5 文档同步（docs/sdk.md、doc-21 §3/§4）与全量测试/ruff/mypy 通过
- [x] #6 窗口/水位越界校验：窗口终点不得晚于最近已收盘交易日（落库日历 + 16:30 CST 截止；显式越界抛 invalid_window，不静默截断），防止水位推进到未来
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. runtime/calendar.py：新增 StoredTradeCalendar（落库日历实现 TradeCalendar 协议：is_trading_day / last_closed；16:30 CST 发布截止；时钟可注入；日历不可用返回 None）
2. control/（新包）：errors.py（IntentError 基类 + JobNotRegistered / InvalidWindow / IntentTimeout）+ client.py（ControlClient：ensure / materialize / run + ControlRun / ControlRuns.wait；幂等 = request_id + job_key；窗口缺省 start=水位+1、end=最近已收盘；显式 end 超界抛 invalid_window；只写 meta.job_runs）
3. derived：errors.py 增 InputStale（code=inputs_stale）；inputs.py 增 input_coverage（PIT 可见事件时间上界，可按实体过滤）；factor_api.py 按需求值前校验数据输入覆盖（窗口终点超出 → inputs_stale，含触发同步 hint）
4. REST：api/routers/jobs.py /jobs/sync 改用 ControlClient（缺省 end=最近已收盘；超界逐项标注）；新增 POST /jobs/materialize（幂等、返回 run）；api/schemas.py 增模型
5. 测试：tests/test_platform_control.py（幂等/缺省窗口/越界/物化版本维度/wait 超时与失败/仅写 meta）+ inputs_stale 用例（不足/充足/实体过滤）+ test_platform_api.py 扩展（sync 口径、materialize 幂等）+ StoredTradeCalendar（截止/未导入）
6. 文档：docs/sdk.md 控制面意图、doc-21 §2/§4（invalid_window/inputs_stale 措辞）、docs/configuration.md 按需
7. 全量 pytest + ruff + mypy；任务卡记录
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
范围补充（TASK-3.25 收尾时确认）：承接「读取路径输入滞后校验」——窗口超出输入数据集水位时抛结构化异常 inputs_stale（含触发同步/回填的可执行提示），与 Factor API 的 as_of/覆盖校验配套。

范围补充（TASK-3.29 实测发现）：手动提交的窗口终点晚于最近已收盘交易日会把水位推到未来，导致调度器无窗口可投（当时人工重置水位修复）；ensure / materialize 需按交易日历钳制窗口终点或显式报错，并补测试（AC#6）。

实现完成（工作区未提交，feat/control-intent-api 分支）：① runtime/calendar.py 新增 StoredTradeCalendar（落库日历实现 TradeCalendar 协议；16:30 CST 截止；表缺失/未覆盖 fail-open → last_closed None）；② 新包 control/（errors + client）：ControlClient.ensure / materialize / run + ControlRun/ControlRuns.wait（轮询终态；超时抛 IntentTimeout 且任务继续）；幂等 = request_id 命中（matched_via=request_id）→ job_key 去重（matched_via=job_key；失败运行不拦截，重复提交即重试）；窗口缺省 = 水位+1 ~ 最近已收盘（日历不可用回退今天），显式终点越界抛 InvalidWindow（不静默截断）；materialize 解析 dataset.output、校验 derive 任务注册、版本维度取算法台账 id@vN、窗口 = 触发日（与调度同键幂等）；只写 meta.job_runs（测试断言数据表 0 行）。③ runtime/repository.py：MetaRepository 增 find_run_by_job_key（SQL + 内存实现）。④ derived：errors 增 InputStale（code=inputs_stale）；inputs 增 input_coverage（PIT 可见事件时间上界，可按实体过滤）与 ensure_inputs_covered；factor_api 按需求值前校验数据输入覆盖。⑤ REST：/jobs/sync 改用 ControlClient（缺省 end = 最近已收盘、显式越界 422、逐项 note 保留）；新增 POST /jobs/materialize（幂等；因子不存在 404 / 未注册 409）。⑥ 测试：新增 tests/test_platform_control.py 12 项；test_platform_api.py 增 4 项（缺省终点/越界 422/materialize 幂等/错误码）+ 夹具补日历表；test_platform_factor_api.py 增 inputs_stale 用例（充足/越界/实体过滤）。⑦ 文档：docs/sdk.md（示例/语义/错误码）与 doc-21 §3/§4（经 backlog CLI）。验证：全量单测 + ruff + mypy(119 文件) 全绿。

评审修复（4 项）：① [中] 多代码 + request_id 句柄错配 → ensure 组合时抛 InvalidRequest（code=invalid_request，不产生半提交，实测 0 运行）；REST /jobs/sync 同组合 → 422（旧实现同场景会把后续代码静默归属到首个运行）；② [低] input_coverage 实体过滤遇非 entity_id 业务键改抛结构化 UnknownField（与访问面一致，原为裸 ValueError）；③ [低] entities=[] 短路不判输入滞后（与对齐读取空结果语义一致）；④ [低] StoredTradeCalendar 改用 inspect().has_table 判表存在：仅表缺失才 fail-open，连接类错误正常抛出。测试：control +2、API +1、factor API +1；文档 docs/sdk.md 与 doc-21 同步 request_id 单代码约束与 invalid_request。验证：全量单测 + ruff + mypy(119 文件) 全绿。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
控制面意图 API 落地：新包 control/（ControlClient.ensure / materialize / run + ControlRun/ControlRuns.wait）——幂等（request_id 单代码命中 / 同窗口 job_key 去重，失败运行不拦截重试）、窗口口径（缺省 = 水位+1 ~ 最近已收盘；显式越界 invalid_window）、物化（derive 任务解析 + 算法身份 id@vN + 触发日窗口）、只写 meta.job_runs（客户端无数据写路径）；REST /jobs/sync 与新增 /jobs/materialize 为同一实现的薄封装；runtime 增 StoredTradeCalendar（has_table 判存在，fail-open 仅限表缺失）与 MetaRepository.find_run_by_job_key；读取路径新增 inputs_stale（数据输入 PIT 可见覆盖校验，空实体集短路）。验证：全量单测 + ruff + mypy(119 文件) 全绿；tests/test_platform_control.py 15 项 + API / Factor API 扩展用例（合并后 main 复跑）；文档 docs/sdk.md 与 doc-21 §3/§4。
<!-- SECTION:FINAL_SUMMARY:END -->
