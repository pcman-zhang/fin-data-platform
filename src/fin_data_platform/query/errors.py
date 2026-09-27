"""查询错误（doc-12 §5 错误码；REST 层映射为 RFC 9457，SDK 复用同一 code/hint）。"""

from __future__ import annotations


class QueryError(Exception):
    """查询错误基类：``code`` 对应 doc-12 §5，``hint`` 为可执行提示。"""

    code = "invalid_query"
    status = 422

    def __init__(self, detail: str, *, hint: str = "") -> None:
        self.detail = detail
        self.hint = hint
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.detail}（{self.hint}）" if self.hint else self.detail


class InvalidDataset(QueryError):
    code = "invalid_dataset"
    status = 404


class InvalidField(QueryError):
    code = "invalid_field"


class VersionModeRequired(QueryError):
    code = "version_mode_required"


class InvalidVersionMode(QueryError):
    code = "invalid_version_mode"


class AsOfRequired(QueryError):
    code = "as_of_required"


class InvalidAsOf(QueryError):
    code = "invalid_as_of"


class InvalidAsOfPolicy(QueryError):
    code = "invalid_as_of_policy"


class PublishTimeMissing(QueryError):
    code = "publish_time_missing"


class UnsupportedFilter(QueryError):
    code = "unsupported_filter"


class NotFound(QueryError):
    code = "not_found"
    status = 404


class ArrowUnavailable(QueryError):
    """Arrow IPC 序列化不可用（未安装 pyarrow）。"""

    code = "arrow_unavailable"
    status = 501
