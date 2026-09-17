# FinDataPlatform（金融数据基础设施）

> 一个具备 Point-In-Time（PIT）语义的金融数据底座：
> 把"正确的数据、正确的时间、正确的身份"固化在平台里，
> 让研究、回测与下游系统消费的是**可以自证的数据**。

多源金融数据真正的难点，从来不是把数据抓下来。一条 2020 年的财务数据，可能到 2021 年才公告，
到 2022 年又被重述；同一家公司在不同源里有不同的代码，而代码会变更、会复用；
每个源的字段口径、复权方式与币种都不相同。抓取脚本能解决"拿到数据"，
却把时间、身份与口径的泥潭留给了使用者。

这个项目选择把泥潭留在平台内部：数据进入时携带完整的时间语义与来源溯源，
落库前经过统一契约的归一化，身份由实体注册表稳定管理，派生数据记录它的算法版本，
最终经由语义版本化的只读出口对外提供。读的人不必知道数据从哪里来、中间洗过几次，
只需要知道：**每个数值都能回答它从哪来、何时可知、用什么算法算的。**

这不是一个应用后端，而是一层数据基础设施——契约、身份、时间、存储与快照才是主体，
API 只是出口。

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
- **访问面**（Access）：Raw / Factor 的统一读取层——PIT（as-of）语义 + 复权等口径组合，
  **严格对齐、读不写库**；派生计算与消费出口共用同一实现；
- **控制面**：Runtime 常驻进程负责调度、依赖、水位、重试与运行记录；
  回填 / 物化均为**控制面意图**（客户端只提交意图，数据写入一律平台内执行）；
- **消费面**：SDK（访问面 + 消费面 + 控制面意图）/ REST / 批量导出，
  其中语义版本化的 Read Model 是默认消费出口。

## 交付形态

平台以容器方式交付：单镜像多入口（控制面 / 调度 / 执行）加数据库与缓存，
由 Docker Compose 编排，一键起服务并自动执行迁移，配置与凭证经环境变量注入；
使用与部署方式见 [配置手册](docs/configuration.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [系统理念](docs/philosophy.md) | 为什么这样设计：六条信条与设计后果 |
| [系统架构](docs/architecture.md) | 分层、平面职责、数据分层、存储与 PIT、控制面、部署 |
| [核心组件](docs/components.md) | 每个组件的作用、边界与代码位置 |
| [SDK](docs/sdk.md) | 访问面 / 因子 / 消费面与控制面意图的接口契约与示例 |
| [数据源](docs/data-sources.md) | 各源当前状态、能力边界、已知问题与对账结论 |
| [配置手册](docs/configuration.md) | 凭证注入、缓存 / 限流 / 预算、数据库与迁移、Runtime 配置 |
| [排障指南](docs/troubleshooting.md) | 诊断顺序与常见故障处置 |

## 边界与路线图

| 阶段 | 范围 | 说明 |
|---|---|---|
| **V1** | Dictionary · Storage · Entity Registry · PIT · Runtime · Derived Engine · Read Model · REST · SDK | 平台主体：数据契约、身份与时间语义、控制面、消费出口 |
| **V1.5（仅预留接口）** | Knowledge Provider · RAG Provider · Agent Provider · Factor Provider | 只有抽象契约，没有实现 |
| **V2** | FIN-RAG · Research Copilot · Natural Language Query · Portfolio Assistant | 平台范围内的智能应用 |
| **永不进入平台** | Alpha Factor · Backtest Engine · Execution Engine · Broker Gateway | 属于消费侧的量化投研平台 |

平台只负责「正确的数据、正确的时间语义、正确的身份语义、正确的派生语义」；
因子挖掘、回测、执行与交易网关永不在本平台内实现。

## 当前状态

| 能力 | 状态 |
|---|---|
| 数据字典（契约 / CI 校验 / 数据目录 / 血缘） | ✅ 已落地 |
| 实体注册表（身份 / 关系 / 外部标识 / PIT Universe） | ✅ 已落地 |
| PIT 存储（schema 生成 / 幂等写入 / as-of 读取 / 实体读模型） | ✅ 已落地 |
| 版本化迁移（基线由字典生成，升级 / 回滚） | ✅ 已落地 |
| FinDataRuntime（调度 / 分发 / 执行 / 运行记录 / 水位 / 重试） | ✅ 已落地（采集同步闭环） |
| 数据源接入（多源适配 / Router / 限流 / 缓存 / 计量） | ✅ 可用 |
| 派生引擎 / 时序查询能力 | 🚧 建设中 |
| 访问面（Raw / Factor API） | 🚧 契约已定稿（[SDK 文档](docs/sdk.md)），实现建设中 |
| 数据质量检查（完整性 / 唯一性 / 时效性 / 对账） | 🚧 建设中 |
| 消费层（SDK / REST / 管理台 / 批量导出） | 🚧 建设中 |
| 部署编排（Docker Compose：数据库 / 服务） | 🚧 仅开发数据库，应用编排规划中 |

> 项目处于开发阶段，接口与契约仍在演进；暂无外部使用者。

## 开发

```
src/fin_data_hub/        接入层（数据源适配、Router、限流、缓存）
src/fin_data_platform/   平台层（dictionary / registry / storage / runtime / ingestion）
migrations/              版本化迁移（基线由数据字典生成）
tests/                   单元测试与集成测试（-m integration）
docs/                    对外文档
```

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,platform,tushare,akshare,ifind,wind,fuyao,baostock]"

.venv/bin/python -m pytest                    # 离线单测（默认跳过集成）
.venv/bin/python -m pytest -m integration     # 端到端（需凭证 / 数据库，见配置手册）
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
```

## 许可证

本项目采用 [MIT License](LICENSE)。

Copyright (c) 2026 Freeman Zhang
