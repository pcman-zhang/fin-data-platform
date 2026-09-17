---
id: TASK-3.24
title: 访问面：Raw 规范化读取 API（PIT + 复权口径组合）
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-17 14:34'
updated_date: '2026-09-17 15:19'
labels: []
milestone: m-0
dependencies:
  - TASK-3.3
  - TASK-3.12
parent_task_id: TASK-3
ordinal: 63000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
在 Canonical 与消费方之间落地访问层：统一的 Raw 读取 API（PIT + 口径组合），派生引擎与消费出口共用同一实现；复权组合只实现一次（口径与数据源 Router 一致）。契约见 doc-21《SDK 接口面与契约》、doc-10 §3.5 口径归属。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 访问面 API 落地（fin_data_platform/access）：read(dataset, fields, *, as_of, adjust=None|qfq|hfq|raw, entities, window) -> Arrow + 元数据
- [ ] #2 字典增机器可读 adjust 声明（modes / factor_dataset / default）+ CI 校验；daily_bar 默认 qfq、指数类 none
- [ ] #3 derived/inputs.py 改为经访问面读取（算法不再自拼复权）；测试覆盖 qfq/hfq/raw 口径、as_of 锚点、不支持组合报错
- [ ] #4 文档同步（doc-10/11/13 + docs 公开页）与全量测试/ruff/mypy 通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 字典 schema：DatasetSpec 增可选 adjust 块（modes / factor_dataset / fields / default）+ CI 校验（default ∈ modes、factor_dataset 存在、fields 在本数据集）；daily_bar 登记 modes [qfq,hfq]、fields [open,high,low,close]、default qfq；指数/财务类不登记（=无复权）
2. access 包（fin_data_platform/access/）：read(...) = PIT 读取（as_of + 业务键最高版本 + 窗口/实体过滤）+ 复权组合（qfq/hfq，锚点=as_of 可见因子最新 trade_date）；返回 Arrow + 元数据（dataset/as_of/adjust/semantic_version）
3. derived/inputs.py 改为经 access 读取（算法输入默认按字典口径复权；显式 adjust="raw" 取原始值），删除重复 SQL 构造
4. 异常：unsupported_adjust（数据集未声明或口径不在 modes）；不静默替换
5. 测试：数值正确性（qfq/hfq/raw、as_of 锚点、重述版本）、字典 CI 用例、不支持口径报错；PG 集成覆盖
6. 文档：doc-11（adjust 字段规范）、doc-13（不落存储）、doc-10 §3.5 引用；全量 pytest/ruff/mypy
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现完成（未提交）：① 字典 AdjustSpec（modes/factor_dataset/factor_field/fields/default）+ 模型级校验 + CI 交叉校验（default∈modes∪none、因子数据集/字段存在、fields 存在、业务键子集）+ daily_bar 登记（qfq/hfq、可复权 [open,high,low,close]、default qfq）+ JSON Schema 重导出；② 新增 access 包：read()（PIT as-of + 复权组合，返回 Arrow + ReadMeta）、read_sql(literal=True 供内联；与按需读取同一 SQL）、结构化异常 AccessError/UnsupportedAdjust/UnknownDataset/UnknownField；复权组合在 SQL 中一次实现（LEFT JOIN 因子 + 每实体最新锚点窗口）；③ derived/inputs 改为经访问面：引用语法 dataset.field[@raw|@qfq|@hfq]（缺省字典口径），视图名安全化（含 @）；engine.inline_sql 改用 inline_input_sql（与 execute 同源）；④ 测试：tests/test_platform_access.py 10 项（缺省口径/raw·hfq 数值、as-of 锚点与未来知识、重述去重、非可复权字段透传、不支持口径/未知数据集字段、内联与按需一致、输入后缀解析与缺省复权、字典 CI 两类）+ PG 集成 1 项（真库 qfq）；⑤ 文档：doc-11 §3.7 adjust 规范与 dataset 表行、doc-10 §3.5 口径归属指向字典声明。验证：471 单测 + PG 集成 6 passed + ruff/mypy 通过。

复审修复（9 项，未提交）：① 请求业务键字段产生重复列（read/read_inputs 崩溃）→ dataset_asof_sql 列去重、passthrough 排除业务键、read_inputs 投影去重；② literal+空实体列表生成 IN () → 渲染 1=0（与绑定路径 0 行语义一致）；③ 不可复权字段加 @qfq 被静默透传 → CI 与运行时 parse_ref 一致拒绝；④ ReadMeta 增 adjusted_fields（如实标注实际复权字段；无可复权字段时 factor_dataset=None）；⑤ 纯日期业务键因子生成非法 SQL → CI 要求因子数据集含非事件时间业务键 + 运行时防守；⑥ pit_class 不支持误抛 UnsupportedAdjust → 新增 UnsupportedPitClass（code=unsupported_pit_class，docs/sdk.md 与 doc-12 错误码同步）；⑦ adjust="none" 别名对齐公开契约（docs/sdk.md 写 none，实现此前只认 raw）；⑧ 派生输入缺省口径与显式同口径合并为一次读取（性能）；⑨ 文档引用 §3.6→§3.7、doc-11 §4 补视图命名与二次复权提示、测试断言具体异常类型。验证：477 单测（access 16 项）+ PG 集成 6 passed + ruff/mypy 通过。
<!-- SECTION:NOTES:END -->
