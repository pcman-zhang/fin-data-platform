---
id: TASK-3.8
title: 管理型 WebUI：数据域 / 同步状态 / 质量报告 / 字典浏览 / 配额
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-13 06:07'
updated_date: '2026-09-17 11:18'
labels: []
milestone: m-0
dependencies:
  - TASK-3.21
parent_task_id: TASK-3
ordinal: 27000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
管理控制台：数据域与同步状态总览、数据质量报告、数据字典与血缘浏览、调度任务管理、配额/成本看板；权限模型按设计（只读/运维角色）。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 WebUI 覆盖设计定稿的信息架构（域/同步/质量/字典/任务/配额）
- [ ] #2 权限与审计接入平台鉴权体系
- [ ] #3 随 docker compose 一键部署可用
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 设计原则先行：web/DESIGN.md 定稿（dark-first、全宽、AntD 6.6.4 token 体系；§9 逐组件固化）
2. 工程：antd@6.6.4 + @ant-design/icons + dayjs；移除 Tailwind/lucide；入口装配 ConfigProvider(theme/locale/tooltip.unique) + App + Router
3. 外壳：全宽 Layout（fixed Sider 200/80 + sticky Header 64 + Content 全宽；无 Footer）
4. 页面：总览（Statistic 4 卡 + 健康 + 状态分布 + 水位）；数据集（Tree 280 + Splitter + 详情：元信息/字段/质量/血缘）；实体（服务端分页 Table + Drawer 深链：时间轴/履历/关系/外部标识）；任务（全宽 Table + 380 触发同步表单（校验 + modal 二次确认 + 结果 Alert）+ 水位 + Run Drawer 5s 轮询）
5. 验证：npm build；headless Chromium 四页 + 深链 + 错误分支联调；离线镜像重建（wheelhouse + Dockerfile.offline）+ compose 实测 :8000
6. 文档与任务：DESIGN.md 定稿；用户查看确认后提交/PR
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
范围收敛（2026-09-13，个人平台定位）：认证/授权/SSO 属于生产级能力，分离至 doc-15《认证与授权（增强功能，暂不制作）》；首期 WebUI 仅本机 127.0.0.1、无账号体系；保留高风险操作二次确认与最小事件日志。

决策（2026-09-13）：事件日志不保留（个人平台首期）；通知渠道独立为 doc-16（飞书等，暂不制作）。

范围（2026-09-15，已确认）：WebUI 基于 TASK-3.21 管理 REST 子集；技术栈 React + TS + Vite + Tailwind + lucide + TanStack Query；v1 页面=数据集/实体注册表/任务（+极简总览）；不引图表库（表格+时间轴）；构建采用本地/CI 预构建 web/dist 入镜像（多阶段 Node 构建为备选）。

实施（AntD 重建，未提交）：web/DESIGN.md 定稿（§0-§9.20，含逐组件细则与选型总表）；依赖切至 antd@6.6.4 + @ant-design/icons + dayjs，移除 Tailwind/lucide（vite.config/index.css/package.json 同步调整）；入口装配 ConfigProvider(theme/locale/tooltip.unique)+App+Router（main.tsx），Shell 为全宽 Layout（fixed Sider 200/80 + sticky Header 64 + Content 16/24）；四页重建：总览（Statistic 4 卡/健康/状态分布/水位）、数据集（Tree 280 + Splitter + 元信息/字段/质量/血缘）、实体（服务端分页 Table + Drawer 深链）、任务（全宽 Table + 380 表单：校验对齐 API + modal 二次确认（焦点取消）+ 结果 Alert；水位；Run Drawer 深链 5s 轮询）；共享原语 ui.tsx（StatusBadge 语义映射、Mono/TimeText/Num、ErrorAlert+错误详情 Drawer、RouteBoundary、分页记忆、Drawer 宽度记忆）；formatDateTime 统一 YYYY-MM-DD HH:mm:ss。

验证：npm run build 通过；vite dev + headless Chromium 实测四页与深链（?dataset=/?entity=/?run=）渲染正常、无效 run 走内联 Alert（404 detail）、控制台无 React/AntD 告警；离线镜像重建（宿主 pip wheelhouse 125MB + /tmp/opencode/Dockerfile.offline --build-context wheels）+ docker compose 重建 service，:8000 托管新 SPA（新资源 hash）且 /healthz ok。环境坑：重建 service 时本 shell 残留 DATABASE_USER/DATABASE_HOST 旧值覆盖 .env（compose 插值 shell env > .env），unset 后恢复，容器内 DATABASE_USER=fdp。
<!-- SECTION:NOTES:END -->
