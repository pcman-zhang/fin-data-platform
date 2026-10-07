# 参与开发

> 面向贡献代码 / 文档的开发者：环境、测试、规范与协作纪律。
> 扩展平台能力（数据源 / 数据集 / 因子）见 [扩展开发](extending.md)。

## 1. 环境准备

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,platform,tushare,akshare,ifind,wind,fuyao,baostock]"
```

可选的依赖组：`platform`（数据库 / 迁移 / 调度）、`cache`（Redis / Arrow）、
`derive`（DuckDB / Arrow）、`api`（FastAPI / uvicorn）、`sdk`（客户端）、
各数据源组（`tushare` / `akshare` / `ifind` / `wind` / `fuyao` / `baostock`）。

## 2. 仓库结构

```
src/fin_data_hub/        接入层（数据源适配、Router、限流、缓存、计量）
src/fin_data_platform/   平台层
  ├─ dictionary/         数据契约（YAML + JSON Schema 校验）
  ├─ registry/           实体注册表（身份 / 履历 / 关系 / 外部标识）
  ├─ storage/            PIT 存储（schema 生成 / 读写 / 读模型 / 迁移 / 授权）
  ├─ runtime/            控制面（调度 / 分发 / 执行 / 水位 / 健康）
  ├─ ingestion/          采集任务与装配（同步 / 全市场登记 / 质量 / 导出）
  ├─ quality/            数据质量（规则编译 / 扫描 / 报告）
  ├─ derived/            派生引擎（算法登记 / 计算 / 物化 / 一致性）
  ├─ access/ query/ panel/   访问面与时序查询内核
  ├─ sdk/                SDK（直连 / REST 双模式）
  ├─ api/                管理 API（FastAPI）
  ├─ export/             批量导出
  ├─ cache/ quota/       缓存层 / 平台配额
migrations/              版本化迁移（基线由字典生成）
tests/                   单元测试与集成测试（-m integration）
web/                     管理 WebUI（Vite + React + antd）
docs/                    对外文档（本目录）
```

## 3. 测试

```bash
.venv/bin/python -m pytest                    # 离线单测（默认跳过集成）
.venv/bin/python -m pytest -m integration     # 端到端（需凭证 / 数据库）
.venv/bin/python -m pytest tests/test_platform_quality.py -q   # 单文件
```

- 默认 `addopts` 排除 `integration` 标记；集成测试需要：
  数据库（`DATABASE_*`）、数据源凭证（`FIN_DATA_HUB_*`），
  破坏性迁移测试另需 `FDP_TEST_DATABASE=1`（仅指向开发库）；
- 数据库可用开发编排：`docker compose -f docker-compose.dev.yml up -d`
  （默认端口 15432）；
- 新增行为必须有测试：单测覆盖离线路径；涉及真实数据库 / 外部源的路径
  用 `@pytest.mark.integration` 标记。

## 4. 代码规范

```bash
.venv/bin/python -m ruff check .       # lint（含 import 排序）
.venv/bin/python -m mypy               # 类型检查（src 全量）
```

前端（`web/`）：

```bash
cd web && npm run typecheck && npm run build   # 构建产物 web/dist 随提交
```

约定：类型注解完整、公开接口写 docstring（说明口径与边界）、
异常带结构化信息（`code / detail / hint`）、不做静默降级。

## 5. 契约与迁移纪律（Schema First）

1. **先改字典**：新增 / 修改数据集在 `src/fin_data_platform/dictionary/` 声明，
   经 `dictionary.schema.json` 校验；
2. **再生成迁移**：数据库 DDL 由字典派生；字典变更必须新增迁移修订，
   升级可重复、可回滚；
3. **漂移校验**：字典与数据库 schema 必须一致（迁移测试自动校验）；
4. **语义版本**：新增字段不升版本；字段含义 / 口径 / 单位变化升
   `semantic_version`，新旧并存过渡。

只读授权（`python -m fin_data_platform.storage.grants`）在迁移后执行，幂等。

## 6. 分支与提交纪律

- **不在 `main` 上直接修改**：任何改动（代码、文档、任务）必须在新分支进行，
  经 PR 合并回 `main`；
- 分支命名：`feat/*` / `fix/*` / `docs/*` / `chore/*`；
- **契约与迁移必须同 PR**；不得提交凭证、本地配置（`.env`）、构建缓存；
- 提交信息说明"做了什么 + 为什么"，一次提交保持单一意图；
- 合并后同步：`git fetch --prune` → 更新本地 `main` → 删除已合并分支。

## 7. 版本与兼容

- SDK 与平台同版本发布；兼容矩阵（SDK 版本 → schema 修订区间）为单一事实源，
  连接时校验、不兼容明确报错；
- 弃用流程：字段 / 语义变更先标注 `deprecated + sunset`，语义变更升版本、
  新旧并存过渡后弃用；
- 读模型 `_vN` 对调用方透明（URL 不变）。

## 8. 文档

- 对外文档在 `docs/`（本目录），每主题一篇，按「理念 → 架构 → 组件 → 使用 →
  配置 → 排障」组织；
- 写作纪律：**自洽**（不引用内部任务 / 设计编号）、不含凭证与个人路径、
  仓库内引用用相对链接、示例可直接执行；
- 修改行为时同步更新文档；文档变更与代码变更可同 PR。

---

延伸阅读：[扩展开发](extending.md) · [配置手册](configuration.md) ·
[系统架构](architecture.md)
