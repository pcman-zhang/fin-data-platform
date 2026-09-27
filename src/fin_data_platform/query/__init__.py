"""PIT 行查询内核（doc-12）：REST 数据面与未来 SDK 共用；不依赖 FastAPI。"""

from fin_data_platform.query.errors import (
    ArrowUnavailable,
    AsOfRequired,
    InvalidAsOf,
    InvalidAsOfPolicy,
    InvalidDataset,
    InvalidField,
    InvalidVersionMode,
    NotFound,
    PublishTimeMissing,
    QueryError,
    UnsupportedFilter,
    VersionModeRequired,
)
from fin_data_platform.query.models import (
    AS_OF_POLICIES,
    FALLBACK_MODES,
    VERSION_MODES,
    FilterClause,
    RowsMeta,
    RowsQuery,
    RowsResult,
)
from fin_data_platform.query.reader import (
    DEFAULT_LIMIT,
    FILTER_OPS,
    MAX_LIMIT,
    platform_metadata,
    read_rows,
)

__all__ = [
    "AS_OF_POLICIES",
    "DEFAULT_LIMIT",
    "FALLBACK_MODES",
    "FILTER_OPS",
    "MAX_LIMIT",
    "VERSION_MODES",
    "AsOfRequired",
    "ArrowUnavailable",
    "FilterClause",
    "InvalidAsOf",
    "InvalidAsOfPolicy",
    "InvalidDataset",
    "InvalidField",
    "InvalidVersionMode",
    "NotFound",
    "PublishTimeMissing",
    "QueryError",
    "RowsMeta",
    "RowsQuery",
    "RowsResult",
    "UnsupportedFilter",
    "VersionModeRequired",
    "platform_metadata",
    "read_rows",
]
