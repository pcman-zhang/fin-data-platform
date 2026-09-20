"""平台写入端（ingestion）：FinDataHub → Canonical 的同步引擎与任务注册。"""

from fin_data_platform.ingestion.adj_factor import (
    DATASET as ADJ_FACTOR_DATASET,
)
from fin_data_platform.ingestion.adj_factor import (
    sync_adjust_factor,
)
from fin_data_platform.ingestion.bootstrap import build_hub, build_sync_runtime
from fin_data_platform.ingestion.daily_bar import DATASET as DAILY_BAR_DATASET
from fin_data_platform.ingestion.daily_bar import SyncResult, sync_daily_bar
from fin_data_platform.ingestion.settings import SyncSettings
from fin_data_platform.ingestion.tasks import (
    register_adj_factor_task,
    register_daily_bar_task,
)

__all__ = [
    "ADJ_FACTOR_DATASET",
    "DAILY_BAR_DATASET",
    "SyncResult",
    "SyncSettings",
    "build_hub",
    "build_sync_runtime",
    "register_adj_factor_task",
    "register_daily_bar_task",
    "sync_adjust_factor",
    "sync_daily_bar",
]
