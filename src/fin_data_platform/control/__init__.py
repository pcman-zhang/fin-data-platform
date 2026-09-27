"""控制面意图（TASK-3.26）：回填与物化的唯一入口。

入口 :class:`ControlClient`：``ensure``（采集/回填）/ ``materialize``（因子物化），
返回可 ``wait`` 的运行句柄；客户端只写 ``meta`` 队列，数据写入由 Runtime 执行。
"""

from __future__ import annotations

from fin_data_platform.control.client import ControlClient, ControlRun, ControlRuns
from fin_data_platform.control.errors import (
    IntentError,
    IntentTimeout,
    InvalidRequest,
    InvalidWindow,
    JobNotRegistered,
    UnknownFactor,
)

__all__ = [
    "ControlClient",
    "ControlRun",
    "ControlRuns",
    "IntentError",
    "IntentTimeout",
    "InvalidRequest",
    "InvalidWindow",
    "JobNotRegistered",
    "UnknownFactor",
]
