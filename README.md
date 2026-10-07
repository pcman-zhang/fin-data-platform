# FinDataPlatform（金融数据基础设施）

> 一个具备 Point-In-Time（PIT）语义的金融数据底座：
> 把"正确的数据、正确的时间、正确的身份"固化在平台里，
> 让研究、回测与下游系统消费的是**可以自证的数据**。

本文写给**金融系统开发者**：如果你在构建研究、回测、监控或下游数据系统，
需要一层可信的数据底座，这里说明它解决什么问题、如何设计、怎么用。

## 为什么需要它

多源金融数据真正的难点，从来不是把数据抓下来：

- **时间**：一条 2020 年的财务数据，可能 2021 年才公告，2022 年又被重述。
  不记录"当时市场知道什么"，任何回测都会前视；
- **身份**：同一家公司在不同源里有不同代码，代码会变更、会复用。
  用代码做跨源主键，历史一断，所有拼接都错；
- **口径**：各源字段含义、单位、复权方式、币种不同，
  横向拼接得到的是"看起来能用"的数据，而不是可以下结论的数据。

本平台把这些问题一次性留在平台内部：数据进入时携带完整时间语义与来源溯源，
落库前经统一契约归一化，身份由实体注册表稳定管理，派生数据记录算法版本，
最终经由语义版本化的只读出口对外提供。**每个数值都能回答它从哪来、
何时可知、用什么算法算的。**

设计取舍的完整论述见 [系统理念](docs/philosophy.md)。

## 架构一览

```
                 ┌──────────────┐
                 │ Data Sources │   Tushare / Wind / iFinD / AkShare / Fuyao / BaoStock
                 └──────┬───────┘
                        │
                  FinDataHub            接入层：唯一源访问面（适配 / Router / 限流 / 缓存 / 计量）
                        │
        ┌───────────────┼───────────────┐
        │               │               │
   Dictionary      Entity Registry    Storage
   数据契约          实体身份          Raw → Canonical → Read Model
        │               │               │
        └───────────────┼───────────────┘
                        │
                  Derived Engine        派生：算法登记 / as-of 输入 / 重述台账
                        │
                    Access              访问面：Raw / Factor 读取（PIT + 口径组合）
                        │
                    Read Models         语义版本化的只读出口
                        │
        ┌───────────────┼───────────────┬───────────┐
        │               │               │           │
       SDK            REST           Export      (MCP…)
```

- **接入层**（FinDataHub）：显式 `source`、跨源合成、限流与缓存，只归一化、不落数据；
- **数据面**：Dictionary（机读契约，Schema First）、Entity Registry（实体身份与关系）、
  Storage（Raw → Canonical → Read Model 分层，事件时间与知识时间双轴）；
- **访问面**（Access）：Raw / Factor / 时序查询的统一读取层——PIT（as-of）语义 +
  复权等口径组合，可选交易日历对齐（交易日 × 标的、停牌 / ST 状态与缺失标注），
  **严格对齐、读不写库**；派生计算与消费出口共用同一实现；
- **控制面**：Runtime 常驻进程负责调度、依赖、水位、重试与运行记录；
  回填 / 物化 / 全局任务均为**控制面意图**（客户端只提交意图，数据写入一律平台内执行）；
- **消费面**：SDK（三个读面 + 意图面）/ REST / 批量导出，
  语义版本化的 Read Model 是默认消费出口。

结构与机制详解见 [系统架构](docs/architecture.md)，组件与扩展点见
[核心组件](docs/components.md)。

## 快速开始

```bash
cp .env.example .env      # 填写数据库密码、数据源凭证与首个同步标的
docker compose up -d      # 数据库 → 迁移 → 控制面 → 服务
curl -s http://127.0.0.1:8000/healthz
```

浏览器打开 `http://127.0.0.1:8000` 查看总览（任务 / 水位 / 质量 / 配额），
交互式 API 文档在 `/api/docs`。完整路径见 [快速开始](docs/getting-started.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [系统理念](docs/philosophy.md) | 为什么这样设计：五类失败、六条信条与设计后果 |
| [系统架构](docs/architecture.md) | 分层与平面、一条数据的一生、PIT 机制、控制面与部署 |
| [核心组件](docs/components.md) | 每个组件的作用、机制、边界与扩展点 |
| [快速开始](docs/getting-started.md) | 部署 → 首个同步 → 查询的 30 分钟路径 |
| [使用指南](docs/usage.md) | 常见工作流：接入 / 查询 / 派生 / 质量 / 导出 / 配额 |
| [SDK](docs/sdk.md) | 三个读面 + 意图面的接口契约、示例与错误模型 |
| [配置手册](docs/configuration.md) | 全部环境变量、数据库与迁移、部署、Runtime 任务、配额 |
| [数据源](docs/data-sources.md) | 各源能力 / 状态 / 对账结论 / 已知问题 |
| [扩展开发](docs/extending.md) | 新增数据源 / 数据集 / 因子 / 质量检查 |
| [排障指南](docs/troubleshooting.md) | 诊断顺序与常见故障处置 |
| [参与开发](docs/development.md) | 环境、测试、规范、分支与提交纪律 |

## 边界与路线图

| 阶段 | 范围 | 说明 |
|---|---|---|
| **V1** | Dictionary · Storage · Entity Registry · PIT · Runtime · Derived Engine · 时序查询 · Read Model · REST · SDK · 质量 · 导出 · 配额 | 平台主体：契约、身份与时间语义、控制面、消费出口 |
| **V1.5（规划方向）** | Knowledge Provider · RAG Provider · Agent Provider · Factor Provider | 尚未预留代码接口 |
| **V2** | FIN-RAG · Research Copilot · Natural Language Query · Portfolio Assistant | 平台范围内的智能应用 |
| **永不进入平台** | Alpha Factor · Backtest Engine · Execution Engine · Broker Gateway | 属于消费侧的量化投研平台 |

平台只负责「正确的数据、正确的时间语义、正确的身份语义、正确的派生语义」；
因子挖掘、回测、执行与交易网关永不在本平台内实现。

## 当前状态

| 能力 | 状态 |
|---|---|
| 数据字典（契约 / CI 校验 / 数据目录 / 血缘） | ✅ 已落地 |
| 实体注册表（身份 / 关系 / 外部标识 / 全市场登记 / PIT Universe） | ✅ 已落地 |
| PIT 存储（schema 生成 / 幂等写入 / as-of 读取 / 实体读模型） | ✅ 已落地 |
| 版本化迁移（基线由字典生成，升级 / 回滚） | ✅ 已落地 |
| FinDataRuntime（调度 / 分发 / 执行 / 运行记录 / 水位 / 重试） | ✅ 已落地（采集同步闭环） |
| 控制面意图（回填 ensure / 物化 materialize / 全局任务 trigger） | ✅ 已落地（幂等 · 审计） |
| 数据源接入（多源适配 / Router / 限流 / 缓存 / 计量） | ✅ 可用 |
| 派生引擎（算法登记 / as-of 输入 / 重述台账） | ✅ 已落地 |
| 访问面 · Raw（PIT / 复权等口径组合 / 交易日历对齐 / 状态与缺失标注） | ✅ 已落地 |
| 访问面 · Factor（严格 as-of / 按需求值 / 物化投影） | ✅ 已落地 |
| 访问面 · 时序查询（范围序列 / 重采样 / 缺口策略 / 窗口算子 / vintage / asof join） | ✅ 已落地 |
| 管理 API 与 WebUI（数据集 / 实体 / 任务 / 质量 / 算法 / 全局任务触发） | ✅ 已落地 |
| 数据面 REST（PIT 行 / Raw / Factor；游标 / ETag / Arrow / gzip） | ✅ 已落地 |
| SDK（pip 安装 / 直连与 REST 双模式 / schema 兼容校验） | ✅ 已落地 |
| 数据质量检查（完整性 / 唯一性 / 时效性 / 对账 / 异常值） | ✅ 已落地（每日报告 · 阈值告警） |
| 批量导出与研究快照（Parquet / Arrow 异步导出） | ✅ 已落地（研究快照预留 v1.1+） |
| 跨进程配额（共享限流 / 成本预算聚合 / `GET /v1/usage`） | ✅ 已落地（缓存不可用 fail-open） |
| 部署编排（Docker Compose：数据库 / 缓存 / 迁移 / 服务） | ✅ 已落地（已实测一键部署） |

> 项目处于开发阶段，接口与契约仍在演进；暂无外部使用者。

## 开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,platform,tushare,akshare,ifind,wind,fuyao,baostock]"

.venv/bin/python -m pytest                    # 离线单测（默认跳过集成）
.venv/bin/python -m pytest -m integration     # 端到端（需凭证 / 数据库）
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
```

贡献流程与规范见 [参与开发](docs/development.md)；扩展平台能力见
[扩展开发](docs/extending.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。

Copyright (c) 2026 Freeman Zhang
