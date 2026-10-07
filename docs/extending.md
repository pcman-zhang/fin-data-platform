# 扩展开发：新增数据源、数据集、因子与检查

> 本文面向**要扩展平台能力**的开发者：每条扩展路径给出机制、改动点与校验方式。
> 所有扩展都遵循同一纪律：**契约先改、代码后改、迁移最后**——
> 数据字典是唯一契约（Schema First）。

## 0. 开发环境与校验基线

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,platform,tushare,akshare,ifind,wind,fuyao,baostock]"

.venv/bin/python -m pytest                    # 离线单测（默认跳过集成）
.venv/bin/python -m pytest -m integration     # 端到端（需凭证 / 数据库）
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
```

任何扩展在提交前必须：字典校验通过、迁移漂移校验通过、全量 pytest / ruff / mypy 全绿。
协作流程（分支 / PR / 评审）见 [参与开发](development.md)。

## 1. 新增数据源适配器

**机制**：接入层（FinDataHub）是唯一源访问面；每个数据源实现一个适配器，
声明**能力集合**，门面据此路由与提前拒绝不支持的请求。

**改动点**：

1. 在 `src/fin_data_hub/enums.py` 登记 `Source` 枚举值；
2. 新建 `src/fin_data_hub/sources/<name>.py`，继承 `BaseAdapter`：

```python
from fin_data_hub.enums import Capability, Source
from fin_data_hub.sources.base import BaseAdapter

class MySourceAdapter(BaseAdapter):
    source = Source.MY_SOURCE
    capabilities = frozenset({Capability.BARS, Capability.TRADE_CALENDAR})

    def fetch_bars(self, codes, *, start, end, freq="1d", adjust=None, **_) -> pd.DataFrame:
        self._acquire("bars")                    # 限流（门面已注入）
        # 调用源端；返回统一 schema 的 DataFrame（列名/类型按 fin_data_hub.schemas）
        # 成功后 self._record("bars", calls=1, codes=tuple(codes), latency_ms=...)
        ...
```

3. 在 `src/fin_data_hub/sources/factory.py` 注册（按凭证装配；缺凭证自动跳过）；
4. 数据字典的 `sources:` 声明新 provider 与字段映射；
   `tests/test_platform_dictionary.py` 会校验字典映射与适配器能力一致。

**边界**：适配器只做"取数与归一"——不落库、不决定存储、不做业务口径判断；
多源合成（补全 / 复权 / 回退）由 Router 统一处理。

**校验**：适配器单测（含限流/计量绑定、schema 一致性）+ 字典映射一致性测试。

## 2. 新增数据集（字典条目）

**机制**：数据集（DataPanel）在 YAML 中声明，经 `dictionary.schema.json` 校验；
数据库 DDL、读模型与生成式文档由契约派生或强校验。

**改动点**：

1. 在 `src/fin_data_platform/dictionary/<domain>/<dataset>.yaml` 新增声明，关键字段：

```yaml
dataset: cn_equity.my_dataset
semantic_version: 1
domain: cn_equity
description: ...
pit_class: market            # market | financial | reference | ...
business_key: [entity_id, trade_date]
physical_key: [entity_id, trade_date, knowledge_time, version]
grain: 标的 × 交易日
update_sla: { frequency: daily, earliest_available: "T+0 18:00", tolerance: "2h" }
sources:
  - provider: tushare
    endpoint: daily
coverage:
  universe_source: cn_equity.listing_lifecycle
  expected_dates: { calendar: ref.trade_calendar, frequency: daily }
storage:
  canonical_table: cn_equity.my_dataset
  read_model: mart.equity_my_dataset_v1
  read_model_impl: view
  partition_strategy: event_time
  partition_interval: 1 month
quality:
  - { rule: unique, keys: [...], severity: error }
  - { rule: not_null, fields: [...], severity: error }
lineage:
  upstream: []
  transform: raw
```

2. 字典校验：

```bash
.venv/bin/python -c "from fin_data_platform.dictionary import validate_directory; print(validate_directory())"
```

3. 生成迁移修订（DDL 由字典 → schema 生成；新数据集以新修订加入，
   升级可重复、可回滚），并跑漂移校验（字典与数据库 schema 必须一致）；
4. 若需要采集：按 §1 的源能力在 `ingestion/tasks.py` 增加任务注册与写入器；
5. 若需要消费：SDK / REST 的读取路径按 `pit_class` 自动适配（无需新增接口）。

**语义版本纪律**：新增字段不升版本；字段含义 / 口径 / 单位变化必须升
`semantic_version`，新旧并存过渡。

## 3. 新增派生算法（因子）

**机制**：算法 = 代码实现 + 带版本的审计标识；输入一律来自访问面（复权等口径
已组合）；依赖随算法登记，决定执行顺序与重算范围。

**改动点**：

1. 实现函数并登记（`src/fin_data_platform/derived/`）：

```python
from collections.abc import Mapping
from datetime import datetime

import pyarrow as pa

from fin_data_platform.derived.registry import register

@register(algorithm_id="my_factor", version=1, owner="my-team")
def my_factor(inputs: Mapping[str, pa.Table], *, as_of: datetime) -> pa.Table:
    """20 日收盘价均线（示例）。

    Formula:
        ``my_factor_t = mean(close_{t-19..t})``（输入声明为 ``close@hfq``）。

    PIT:
        输入由引擎按 ``knowledge_time <= as_of`` 过滤；本函数不重新读数据。
    """
    daily = inputs["cn_equity.daily_bar.close@hfq"]
    # 计算后返回含业务键（entity_id, trade_date）与输出列的 Arrow 表
    result = ...  # pa.Table
    return result
```

引擎以 ``function(inputs, as_of=...)`` 调用实现：输入是引用名 → Arrow 表的映射，
返回值必须是含**业务键 + 输出列**的 ``pyarrow.Table``；docstring 必须带
``Formula:`` / ``PIT:`` 标记（注册校验要求）。

2. 在数据字典声明派生条目（输出字段 / 输入数据集与字段 / 物化模式
   `none` 或 `latest` / 依赖上游算法）；
3. 一致性校验（导入实现、输入输出匹配、上游指纹、依赖无环）由 CI 测试覆盖；
4. 物化：`materialize: latest` 的因子由 `FDP_DERIVE_SCHEDULE` 或
   `control.materialize` 触发；`none` 的按需计算。

**升级纪律**：升级 = 新增 `algorithm_id`（新版本号），旧实现永久保留；
生效日与原因写入重述台账。读取方可用 `algorithm_id` pin 已注册标识
（不同版本以不同标识注册；单份投影不支持历史多版本回溯）。

**校验**：因子端到端测试（as-of 防前视、依赖门控、物化与对齐异常）。

## 4. 新增质量检查类型

**机制**：质量规则在字典声明，由规则编译层编译为 SQL 检查执行。

**改动点**：

1. 若复用现有规则形态（`unique` / `not_null` / `range` / `enum` / `expression` /
   `jump` / `reconcile`），只需在字典声明，无需改代码；
2. 新检查形态：在 `src/fin_data_platform/quality/rules.py` 注册编译器
   （字典字段 → SQL / 检查函数），在 `quality/models.py` 增加结果结构（如需），
   并扩展字典 schema 与校验测试；
3. 在目标数据集的 `quality:` 中声明规则与严重级（`error` / `warn`）。

**边界**：质量层只读扫描与报告，不修改数据、不发明口径——规则文本归字典。

## 5. 开发工作流与校验

| 事项 | 入口 |
|---|---|
| 字典校验 | `dictionary.validate_directory()`（CI 测试同步覆盖） |
| 迁移生成 / 漂移校验 | `storage.migrations`（基线由字典生成；漂移由迁移测试校验） |
| 只读授权 | `python -m fin_data_platform.storage.grants`（幂等） |
| 离线测试 | `.venv/bin/python -m pytest`（默认跳过集成） |
| 集成测试 | `.venv/bin/python -m pytest -m integration`（需凭证 / 数据库） |
| 代码规范 | `ruff check .`、`mypy` |

**提交纪律**：分支开发（`feat/*` / `fix/*` / `docs/*` / `chore/*`），
经 PR 评审合并；**契约与迁移必须同 PR**；不得提交凭证或本地配置。

---

延伸阅读：[核心组件](components.md) · [数据字典格式（schema）](../src/fin_data_platform/dictionary/_schema/dictionary.schema.json) ·
[参与开发](development.md) · [配置手册](configuration.md)
