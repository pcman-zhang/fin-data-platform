---
id: TASK-5
title: clarify-docs：对外文档体系（docs/）与 README 文学化
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-14 14:42'
updated_date: '2026-10-07 15:46'
labels:
  - docs
milestone: m-0
dependencies: []
ordinal: 58000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
现状：README 已完成一次叙事重构（TASK-3.17），但仍偏清单式；对外可读的完整文档尚未建立——系统理念、架构、组件与配置、排障等内容散落在 backlog 设计文档中，外部使用者无法顺畅阅读。

目标：建立**面向使用者的对外文档体系**（仓库根 `docs/`，**不受 backlog 管理**，每个主题一篇），并将 README 文学化、嵌入链接，使读者可按「理念 → 架构 → 组件 → 配置 → 排障」路径自洽阅读。

文档结构（建议，主题各一篇）：
1. `docs/philosophy.md` —— 系统理念：为什么做（PIT / 无前视、Schema First、字典即契约、缓存非权威、算法可复现）；
2. `docs/architecture.md` —— 系统架构：接入层 / 控制面 / 数据面 / 派生 / 消费面与三平面、写入边界；
3. `docs/components.md`（或拆分为 components/*.md）—— 核心组件：FinDataHub Router（多源合成）、Dictionary（数据契约）、Entity Registry（实体身份）、Runtime（控制面）、Storage（PIT）、Derived Engine、Read Model、消费层；
4. `docs/configuration.md` —— 每个组件的作用与配置方法：环境变量、凭证注入方式、调度配置（FDP_SYNC_*）、数据库连接与迁移；
5. `docs/troubleshooting.md` —— 排障：常见失败模式（源失败/缺口/限流/迁移/权限）、诊断命令与检查顺序。

约束（开源纪律）：对外文档必须**自洽**——不引用 backlog 编号（如 doc-10）与内部上下文；不含外部项目名、机器路径、账号/额度/价格、凭证位置；引用仓库内文件用相对链接。

参考资料（重写来源，不直接搬运）：现有 README；backlog 冻结设计（架构总纲、数据字典规范、Runtime 设计、Router Policy）——对外化改写。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 对外 docs/ 目录建立（理念 / 架构 / 核心组件 / 组件配置 / 排障 每主题一篇），不受 backlog 管理
- [x] #2 内容自洽且符合开源纪律：无内部编号依赖、无敏感信息；与冻结设计一致
- [x] #3 README 文学化重写：保持事实准确，嵌入 docs/ 链接（相对链接有效）
- [x] #4 链接检查通过；现有 pytest / ruff / mypy 不受影响
- [ ] #5 持续维护：后续文档修订与新增主题纳入本任务（任务保持开放）
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 复核与取材：README、冻结设计（架构总纲 / 字典规范 / Runtime 设计 / Router Policy）、各模块配置面（环境变量 / 配置对象 / 迁移与 DSN）
2. 建立对外 docs/（理念 / 架构 / 核心组件 / 组件配置 / 排障），每主题一篇、自洽无内部编号
3. README 文学化重写：保持事实准确，嵌入 docs/ 链接
4. 顺修 doc-11 两处遗留（cn_equity.security_master → cn_equity.listing_lifecycle）
5. 验证：链接检查（相对链接可达）+ pytest / ruff / mypy
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现（2026-09-14）：① 新建对外 docs/ 五篇——philosophy（六条信条）/ architecture（分层·三平面·PIT·控制面·部署）/ components（七组件作用·边界·代码位置 + 各源支持矩阵）/ configuration（安装·Hub 配置·数据库与迁移·Runtime 全配置项·测试变量）/ troubleshooting（诊断顺序 + 八类故障处置）；② README 文学化重写：理念叙事 → 架构 → 快速开始 → 文档导航表，移除内部编号引用，链接 docs/；③ 顺修 doc-11 两处遗留（cn_equity.security_master → cn_equity.listing_lifecycle）；④ 验证：链接检查（README+docs 全部相对链接可达）、docs/ 无 doc-/TASK- 内部引用残留、366 单测 + ruff/mypy 全绿。

补充（评审要求）：components.md §3 重写——新增「为什么需要这一层」（代码≠身份 / 公司≠证券 / 关系图 / 外部标识）与「issuer 与 listing 为何分开」（职责对照表、直接收益、非发行实体处理）；philosophy.md 信条 3 增补「公司不等于证券」要点。

增补（用户要求）：新增 docs/data-sources.md（数据源）——概览表、通用机制、6 源详情（能力/状态/已知问题）、对账方法与结论表（Tushare 基准、BaoStock 因子通过、Fuyao 原始价一致但预计算复权不通过）、已知问题汇总表；README 文档导航与 components/configuration 交叉链接更新。复核发现：AkShare 能力表声明 SNAPSHOT 但适配器未实现 fetch_snapshot（调用会抛 NotImplementedError），已记入已知问题，待修复。

修订（用户反馈）：① README 删除「快速开始」（pip 安装 + 库用法 + 手动迁移/启动），改为「交付形态」——容器化部署（单镜像多入口 + Docker Compose，编排建设中）；② 配置手册 §1 同步改为「交付形态与安装」，仅保留源码开发安装。理由：系统最终以 Docker 形式交付，pip/库用法不符合系统设计。

更正（复核）：AkShare 快照问题实际为「元数据不一致」而非运行时缺陷——适配器 capabilities = {BARS, FUND_NAV, TRADE_CALENDAR}，注册表 get_for_capability 会干净拒绝（UnsupportedCapability），此前"NotImplementedError"判断有误；data-sources.md 措辞已更正为「能力表登记与适配器声明不一致，待清理（撤销登记或实现接入）」。

代码清理（用户决定）：撤销 AkShare 的 SNAPSHOT 能力声明（capabilities.py），快照能力由 Wind / Fuyao 提供；data-sources.md 与 components.md 同步（AkShare 不支持快照，并从已知问题表移除该项）。验证：pytest / ruff / mypy 全绿。

数据源文档增补（用户要求）：① 概览表新增「费用」列（Tushare/AkShare/Fuyao/BaoStock 免费；Wind 付费；iFinD 计费按调用）并入通用机制加「费用意识」；② 快照口径改为「Fuyao 主力、暂不配置回退」（Wind 亦具备），capabilities.py 注释与 components.md 同步。验证：pytest / ruff / mypy 全绿，链接全部可达。

任务保持开放（用户决定，2026-09-14）：文档是持续资产，后续修订与新增主题继续在本任务上进行，不设终点。

重写（2026-10-07，用户要求：面向金融系统开发者、详解设计思路与使用方式）：新增 getting-started（部署到首查 30 分钟路径）、usage（接入/查询/派生/质量/导出/配额工作流）、extending（数据源/数据集/因子/质量检查扩展）、development（参与开发）；重写 philosophy（五类失败 + 六条信条）、architecture（一条数据的一生 + PIT/幂等/代际机制）、components（组件职责/机制/扩展点，新增 Quota/Cache 篇）、sdk（接口契约/错误模型/版本兼容）、configuration（全量配置重构，新增派生调度节并重编号）、data-sources（选源建议 + 已知问题）、troubleshooting（诊断顺序 + 消费侧）；README 重写并嵌入 12 篇导航。规模 2557 行。验证：相对链接/锚点脚本检查通过、无内部编号与敏感信息；两轮独立评审（4 高/7 中/10 低 + 复核 1 中/3 低）全部修复后与代码事实一致；pytest EXIT=0 / ruff / mypy 全绿。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
对外文档体系落地：docs/ 五篇（理念 / 架构 / 核心组件 / 配置 / 排障）自洽且符合开源纪律（无内部编号与敏感信息）；README 文学化重写并嵌入文档导航；顺修数据字典规范两处遗留示例（universe_source 指向生命周期数据集）。验证：相对链接全部可达、无内部引用残留、366 单测 + ruff/mypy 全绿。
<!-- SECTION:FINAL_SUMMARY:END -->
