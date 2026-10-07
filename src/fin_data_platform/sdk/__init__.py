"""FinDataPlatform SDK（TASK-3.11 / doc-21）：pip 安装、双模式（直连 / REST）。

```python
from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()
```

- **直连模式**（默认）：只读 DSN 读数据；控制面意图需 ``control_dsn``（meta 写权限）；
- **REST 模式**：``FDP_SDK_MODE=rest`` + ``FDP_SDK_REST_URL``（首期无 API Key）；
- 结果：``.frame``（pandas）/ ``.table``（Arrow，直连）/ ``.meta``（Pydantic，两模式同构）；
- 连接时校验 schema 修订兼容区间（:mod:`fin_data_platform.sdk.compat`）。
"""

from fin_data_platform.sdk.client import FinDataPlatform
from fin_data_platform.sdk.compat import (
    COMPATIBILITY_MATRIX,
    MAX_SCHEMA_REVISION,
    MIN_SCHEMA_REVISION,
    SDK_VERSION,
    check_schema_revision,
)
from fin_data_platform.sdk.config import SdkConfig, SdkMode
from fin_data_platform.sdk.errors import FinDataError
from fin_data_platform.sdk.models import (
    CONTRACT_MODELS,
    ControlRunInfo,
    ErrorModel,
    FactorResultMeta,
    ResultMeta,
    SdkResult,
    SdkRun,
    export_json_schema,
)

__all__ = [
    "COMPATIBILITY_MATRIX",
    "CONTRACT_MODELS",
    "MAX_SCHEMA_REVISION",
    "MIN_SCHEMA_REVISION",
    "SDK_VERSION",
    "ControlRunInfo",
    "ErrorModel",
    "FactorResultMeta",
    "FinDataError",
    "FinDataPlatform",
    "ResultMeta",
    "SdkConfig",
    "SdkMode",
    "SdkResult",
    "SdkRun",
    "check_schema_revision",
    "export_json_schema",
]
