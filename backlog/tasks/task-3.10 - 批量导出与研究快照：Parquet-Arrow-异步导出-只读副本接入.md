---
id: TASK-3.10
title: 批量导出与研究快照 + 只读副本/读模型（对外量化通道）
status: Done
assignee: []
created_date: '2026-09-13 06:21'
updated_date: '2026-10-07 12:47'
labels: []
milestone: m-0
dependencies:
  - TASK-3.3
parent_task_id: TASK-3
ordinal: 29000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
面向外部量化消费的批量与直连通道：① 按域/标的/时间窗异步导出 Parquet/Arrow（对象存储或共享卷 + 下载链接），供回测与研究；② 只读副本 + 稳定读模型（mart/api schema 视图、PIT/as-of 安全、版本化与弃用流程）；③ 按域只读角色与审计、资源隔离（statement_timeout/连接数）；④ 可选 ADBC/Arrow SQL。禁止外部直连内部原始表。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 异步导出可用：任务提交/状态/下载全流程，产物为 Parquet/Arrow
- [x] #2 全市场长历史导出不经过在线 REST 查询通道（避免逐标的拉取）
- [x] #3 外部不直连内部原始表；资源隔离生效
- [x] #4 只读副本暂不考虑（非商业部署）；保留读写 DSN 分离配置供未来拆分
- [x] #5 稳定读模型（PIT/as-of 视图、版本化、数据字典登记）与只读角色/审计落地（单一 fdp_ro；按域角色留待演进——用户决策 2026-10-07）
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
评审通过（2026-10-07），分支 feat/task-3.10-export；范围决策：不做按域只读角色（维持单一 fdp_ro）；研究快照预留不实现（v1.1+）。
1. 导出模块 export/：meta.export_requests（迁移 0008：export_id/dataset/params/status/artifact_path/format/rows/bytes/error/run_id/时间戳）；writer 分块批量写 Parquet/Arrow（实体批 500 × 时间块 90 天，内存有界、不逐标的拉取；PIT 语义复用 query 内核）；tasks 全局任务 export.jobs（kind=export，scope=export_id；Runtime 执行、失败留痕可重试）。
2. REST（doc-12 §2.3）：POST /v1/exports（202；校验 dataset/fields/version_mode 后建请求 + 提交意图）、GET /v1/exports、GET /v1/exports/{id}、GET /v1/exports/{id}/download（未就绪 409）。
3. 资源隔离（AC#4）：StorageConfig.statement_timeout（读连接注入，默认 30s，0=关闭）+ 文档；导出走平台内部连接（不经 REST 在线通道）。
4. 编排：compose exports-data 卷（runtime/service 挂载 /data/exports，FDP_EXPORT_DIR）。
5. 测试：导出任务端到端（sqlite + pyarrow：分块写 / 行数 / 产物可读）、API 流程（提交/状态/下载/错误）、statement_timeout 注入；集成测试（真实 PG 小样本导出）。
6. 文档：configuration（FDP_EXPORT_DIR / statement_timeout）、README（批量导出 ✅）、doc-12 §11、doc-13（0008）。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实施进展（2026-10-08，分支 feat/task-3.10-export）：① export/ 模块：schema（meta.export_requests；迁移 0008 生成 + 台账登记）、store（请求状态机 pending/running/succeeded/failed）、writer（分块批量写 Parquet/Arrow：实体批 500 × 时间块 90 天；无窗口取表内事件时间范围；单块行数达查询上限即报错，不静默截断）、tasks（全局任务 export.jobs，scope=export_id；失败留痕可重试；已完成幂等）。② REST：POST /v1/exports（校验 dataset/fields/version_mode/window + 任务装配检查 → 登记请求 + 提交意图 → 202）、GET /v1/exports、GET /v1/exports/{id}、/download（FileResponse；未就绪 409）；app 增加 IntentError → RFC 9457 处理器。③ 资源隔离：StorageConfig.statement_timeout（读连接注入 -c statement_timeout，默认 30s，0/空=关闭；写端与导出不注入）+ 测试。④ 编排：compose exports-data 卷（runtime/service 挂载 /data/exports）+ FDP_EXPORT_DIR；runtime 装配导出任务（FDP_EXPORT_ENTITY_BATCH / FDP_EXPORT_CHUNK_DAYS）。⑤ 测试：tests/test_platform_export.py 6 项（Parquet/Arrow 写出、分块与实体批、过滤与 PIT、任务端到端 + 失败路径、API 全流程 + 参数校验、statement_timeout 配置与注入）+ 迁移漂移（0008）+ SDK 兼容上限更新（0008）；全量 pytest EXIT=0、ruff、mypy(161 文件) 全绿。⑥ 文档：configuration §3.1/§6.2/§7、README（批量导出 ✅ / 快照预留 v1.1+）、doc-12 §11、doc-13、doc-21 §8。

真实部署发现并修复（2026-10-08）：① **命名卷属主问题**——compose 新卷 exports-data 挂载 /data/exports 时属主为 root，容器以非 root 用户（fdp）运行 → 导出写出 PermissionError（首轮导出 run 29/30 failed，error 留痕正常）；修复：Dockerfile 预建 /data/exports 并 chown fdp（新卷首次创建时继承镜像目录属主），已有卷需删除重建。② 导出任务重试语义实测正常（max_attempts=2，失败后重试并最终 failed，状态/错误可查询）。

独立代码评审（2026-10-08，第二轮）发现并修复（导出测试 11 项 + 部署工件 5 项）：① 【高】空时间块 / 全 NULL 列导致多块写出 schema 错配（Parquet/Arrow 必然失败）→ 目标 schema 由物理表列类型预先构造、空块跳过、逐块 cast。② 【高】compose 拆分角色 runtime-worker 未挂载导出卷（导出成功但无法下载）→ 补挂载 + 部署工件测试断言。③ 【中】entity_batch 参数与 FDP_EXPORT_ENTITY_BATCH 未生效（恒用默认）→ 透传 + spy 测试断言批次。④ 【中】REST 创建的运行绕过 TaskSpec 的 priority/max_attempts → 从镜像任务定义填充 + 断言。⑤ 【中】entities 跨批重复导致结果行重复 → _plan 去重。⑥ 【中】进程被杀后请求永久 running → 启动对账（失联 running → failed）；mark_running 清空 finished_at。⑦ 【低】提交期校验补强（filters 算子/字段、as_of_policy/fallback_mode、entities 适用性）；失败产物改「临时文件 + 原子替换」；单块截断判定改用 next_cursor（恰好 MAX_LIMIT 不误报）；request_id 明确非幂等；version_mode 缺省 latest 标注为便利性例外；statement_timeout 适用范围文档化；compat 提示文案更新 0008。验证：全量 pytest EXIT=0、ruff、mypy(161 文件) 全绿。

收尾（2026-10-08）：AC 1–5 依据 11 项导出单测 + 5 项部署工件测试 + 全量 pytest/ruff/mypy(161) + 真实部署实测（Parquet 86 行 / Arrow 21 行下载读回、首块为空的多块导出、意图参数 200/2、重复 entities 去重、命名卷属主修复复验）+ 集成授权测试（只读角色，含 alembic_version 可读）核对勾选；全部变更已并入 PR #48（fa2e579）。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
交付批量导出与研究快照（doc-12 §2.3）：异步导出任务 export.jobs（scope=export_id；分块批量写 Parquet / Arrow——实体批 × 时间块、目标 schema 由物理表类型构造、空块跳过、失败原子替换、截断即报错、进程重启对账）、REST /v1/exports（提交 / 状态 / 下载）、读连接 statement_timeout 资源隔离、compose 导出卷（runtime / runtime-worker / service）。验证：导出测试 11 项 + 部署工件 5 项 + 全量 pytest EXIT=0 / ruff / mypy(161 文件) 全绿；docker compose 实测（86 行 Parquet / 21 行 Arrow 下载读回、多块与空块、去重、意图参数）；研究快照按 doc-12 预留（v1.1+）；部署期修复命名卷属主缺陷。
<!-- SECTION:FINAL_SUMMARY:END -->
