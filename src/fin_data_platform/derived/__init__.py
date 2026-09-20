"""派生引擎（Derived Engine；doc-10 §3.5 / doc-11 §4）。

原则：**存输入与算法，不存派生结果的多个版本**；派生输出最多保留"最新一份"
可重建投影（缓存性质）。

模块划分：

- :mod:`fin_data_platform.derived.registry`：``@register`` 算法注册表（id / version /
  implementation / owner；CI 校验 docstring Formula/PIT）；
- :mod:`fin_data_platform.derived.consistency`：字典 ``derived`` ↔ 注册表一致性；
- :mod:`fin_data_platform.derived.schema` / :mod:`fin_data_platform.derived.store`：
  ``meta.algorithm_registry`` / ``algorithm_events`` / ``data_generation`` 持久化；
- :mod:`fin_data_platform.derived.sync`：登记同步（CLI：``python -m fin_data_platform.derived``）。
"""

from __future__ import annotations

from fin_data_platform.derived.consistency import (
    check_consistency,
    classify_algorithms,
    import_implementations,
    referenced_algorithms,
)
from fin_data_platform.derived.engine import (
    DerivedEngine,
    DerivedPlan,
    DerivedResult,
    MaterializeReport,
    generation_stamp,
    projection_name,
)
from fin_data_platform.derived.factor_api import FactorAPI, FactorMeta, FactorResult, FactorSummary
from fin_data_platform.derived.registry import (
    DEFAULT_REGISTRY,
    AlgorithmRegistry,
    AlgorithmSpec,
    build_spec,
    register,
)
from fin_data_platform.derived.store import (
    AlgorithmEvent,
    AlgorithmRow,
    AlgorithmStore,
    DataGenerationRow,
    InMemoryAlgorithmStore,
    SqlAlgorithmStore,
)
from fin_data_platform.derived.sync import SyncReport, build_events, build_rows, sync_algorithms

__all__ = [
    "DEFAULT_REGISTRY",
    "FactorAPI",
    "FactorMeta",
    "FactorResult",
    "FactorSummary",
    "DerivedEngine",
    "DerivedPlan",
    "DerivedResult",
    "MaterializeReport",
    "generation_stamp",
    "projection_name",
    "AlgorithmEvent",
    "AlgorithmRegistry",
    "AlgorithmRow",
    "AlgorithmSpec",
    "AlgorithmStore",
    "DataGenerationRow",
    "InMemoryAlgorithmStore",
    "SqlAlgorithmStore",
    "SyncReport",
    "build_events",
    "build_rows",
    "build_spec",
    "check_consistency",
    "classify_algorithms",
    "import_implementations",
    "referenced_algorithms",
    "register",
    "sync_algorithms",
]
