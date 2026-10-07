"""SDK ↔ 数据库 schema 兼容矩阵与连接校验（doc-21 §6，TASK-3.11）。

- :data:`COMPATIBILITY_MATRIX`：SDK 版本 → 支持的 schema 修订区间（单一事实源）；
- :func:`check_schema_revision`：直连模式查 ``alembic_version`` 后校验；
  REST 模式以 ``/v1/health`` 的 ``schema_revision`` 检查（平台侧同口径自检）。
不兼容时抛 ``FinDataError(code="schema_incompatible")``，提示升级方向。
"""

from __future__ import annotations

from fin_data_platform._version import __version__
from fin_data_platform.sdk.errors import FinDataError

#: SDK 版本（与平台同版本发布）
SDK_VERSION = __version__

#: 支持的 schema 修订区间（含端点；新增修订时更新并同步文档）
MIN_SCHEMA_REVISION = "0006_daily_status"
MAX_SCHEMA_REVISION = "0008_export_meta"

#: 兼容矩阵（文档 / 诊断用；SDK 版本 → (min, max)）
COMPATIBILITY_MATRIX: dict[str, tuple[str, str]] = {
    SDK_VERSION: (MIN_SCHEMA_REVISION, MAX_SCHEMA_REVISION),
}


def _revision_number(revision: str) -> int:
    prefix = revision.split("_", 1)[0]
    try:
        return int(prefix)
    except ValueError as exc:  # 修订命名不符合 ``NNNN_*`` 约定
        raise FinDataError(
            "schema_incompatible",
            f"无法解析 schema 修订：{revision!r}",
            hint="alembic_version.version_num 期望形如 0008_export_meta",
        ) from exc


def check_schema_revision(revision: str | None) -> None:
    """校验数据库 schema 修订在兼容区间；不兼容抛 ``schema_incompatible``。"""
    if revision is None:
        raise FinDataError(
            "schema_incompatible",
            "数据库 schema 未知（alembic_version 缺失）",
            hint="先执行迁移（alembic upgrade head）再连接",
        )
    number = _revision_number(revision)
    if number < _revision_number(MIN_SCHEMA_REVISION):
        raise FinDataError(
            "schema_incompatible",
            f"数据库 schema 过旧（{revision}），SDK {SDK_VERSION} 要求 "
            f"{MIN_SCHEMA_REVISION} ~ {MAX_SCHEMA_REVISION}",
            hint="升级数据库：alembic upgrade head",
        )
    if number > _revision_number(MAX_SCHEMA_REVISION):
        raise FinDataError(
            "schema_incompatible",
            f"数据库 schema 较新（{revision}），SDK {SDK_VERSION} 尚不支持",
            hint="升级 SDK（pip install -U fin-data-platform）",
        )
