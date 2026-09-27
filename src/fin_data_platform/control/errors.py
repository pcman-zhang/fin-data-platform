"""控制面意图异常（结构化 code / detail / hint；SDK 与 REST 同构映射，doc-21 §4）。"""

from __future__ import annotations


class IntentError(Exception):
    """控制面意图异常基类。"""

    code = "intent_error"

    def __init__(self, detail: str, *, hint: str = "") -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


class JobNotRegistered(IntentError):
    """目标任务未注册（数据集/代码/因子未随 Runtime 装配）。"""

    code = "job_not_registered"


class UnknownFactor(IntentError):
    """因子不存在或重名（需显式 dataset 消歧）。"""

    code = "not_found"


class InvalidRequest(IntentError):
    """请求语义不支持（如 `request_id` 与多代码提交组合）。"""

    code = "invalid_request"


class InvalidWindow(IntentError):
    """窗口非法（起止颠倒，或终点晚于最近已收盘交易日）。"""

    code = "invalid_window"


class IntentTimeout(IntentError):
    """等待运行终态超时（任务继续在平台侧执行）。"""

    code = "timeout"
