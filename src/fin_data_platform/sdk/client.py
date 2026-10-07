"""FinDataPlatform SDK 门面（doc-21）：双模式（直连 / REST），三读面 + 一意图面。

```python
from fin_data_platform.sdk import FinDataPlatform

fdp = FinDataPlatform.from_env()          # 直连（FDP_SDK_DSN）或 REST（FDP_SDK_MODE=rest）
bars = fdp.raw.read("cn_equity.daily_bar", fields=["close"], entities=[10001],
                    window=(date(2024, 1, 1), date(2024, 12, 31)),
                    as_of=datetime(2025, 1, 1, 12, 0))
factor = fdp.factors.read("ma20", as_of=datetime(2025, 1, 1, 12, 0))
rows = fdp.read_model.read("cn_equity.daily_bar", version_mode="as_of",
                           as_of=datetime(2025, 1, 1, 12, 0), fields=["close"])
run = fdp.control.ensure("cn_equity.daily_bar", codes=["600519.SH"])
status = run.wait(timeout=60)
```
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from fin_data_platform.sdk.config import SdkConfig, SdkMode
from fin_data_platform.sdk.models import SdkResult, SdkRun

if TYPE_CHECKING:  # 延迟导入：REST-only 安装不依赖平台内核
    from fin_data_platform.panel import Rolling


class FinDataPlatform:
    """SDK 入口（``from_env`` 或显式 ``SdkConfig``；``backend`` 可注入供测试）。"""

    def __init__(
        self,
        config: SdkConfig | None = None,
        *,
        backend: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config if config is not None else SdkConfig.from_env(env)
        self._backend = backend if backend is not None else _build_backend(self.config)
        self.raw = RawAccess(self)
        self.factors = FactorAccess(self)
        self.read_model = ReadModelAccess(self)
        self.panel = PanelAccess(self)
        self.control = ControlIntent(self)
        self._checked = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FinDataPlatform:
        return cls(SdkConfig.from_env(env))

    def connect(self) -> FinDataPlatform:
        """执行连接与 schema 兼容校验（幂等；首次调用也会自动执行）。"""
        self._ensure_checked()
        return self

    def close(self) -> None:
        """释放自建资源（HTTP 连接池 / 数据库引擎；注入的资源由调用方管理）。"""
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> FinDataPlatform:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_checked(self) -> None:
        if self._checked or not self.config.check_compatibility:
            return
        self._backend.check()
        self._checked = True

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._ensure_checked()
        return getattr(self._backend, name)(*args, **kwargs)


def _build_backend(config: SdkConfig) -> Any:
    if config.mode == SdkMode.REST:
        from fin_data_platform.sdk.rest import RestBackend

        return RestBackend(config)
    from fin_data_platform.sdk.direct import DirectBackend

    return DirectBackend(config)


class RawAccess:
    """访问面 · Raw：PIT（as_of 必填）+ 口径组合（adjust）+ 可选日历对齐。"""

    def __init__(self, platform: FinDataPlatform) -> None:
        self._platform = platform

    def read(
        self,
        dataset: str,
        *,
        as_of: datetime,
        fields: Sequence[str] | None = None,
        entities: Sequence[int] | None = None,
        window: tuple[date, date] | None = None,
        adjust: str | None = None,
        align_calendar: bool = False,
        limit: int | None = None,
    ) -> SdkResult:
        return self._platform._call(
            "raw_read",
            dataset,
            fields=fields,
            entities=entities,
            window=window,
            adjust=adjust,
            as_of=as_of,
            align_calendar=align_calendar,
            limit=limit,
        )


class FactorAccess:
    """访问面 · Factor：严格 as_of 对齐；可 pin ``algorithm_id`` 复现历史结果。"""

    def __init__(self, platform: FinDataPlatform) -> None:
        self._platform = platform

    def read(
        self,
        output: str,
        *,
        as_of: datetime,
        dataset: str | None = None,
        entities: Sequence[int] | None = None,
        window: tuple[date, date] | None = None,
        algorithm_id: str | None = None,
        limit: int | None = None,
    ) -> SdkResult:
        return self._platform._call(
            "factor_read",
            output,
            dataset=dataset,
            entities=entities,
            window=window,
            as_of=as_of,
            algorithm_id=algorithm_id,
            limit=limit,
        )


class ReadModelAccess:
    """消费面 · Read Model（doc-12 PIT 语义；REST 与直连同构）。"""

    def __init__(self, platform: FinDataPlatform) -> None:
        self._platform = platform

    def read(
        self,
        dataset: str,
        *,
        version_mode: str,
        as_of: datetime | None = None,
        as_of_policy: str = "knowledge",
        fallback_mode: str = "strict",
        entities: Sequence[int] | None = None,
        window: tuple[date, date] | None = None,
        fields: Sequence[str] | None = None,
        filters: Sequence[Mapping[str, Any]] | None = None,
        order_by: Sequence[str] | None = None,
        limit: int = 1000,
        cursor: str | None = None,
        include_meta: bool = False,
    ) -> SdkResult:
        return self._platform._call(
            "read_model",
            dataset,
            version_mode=version_mode,
            as_of=as_of,
            as_of_policy=as_of_policy,
            fallback_mode=fallback_mode,
            entities=entities,
            window=window,
            fields=fields,
            filters=filters,
            order_by=order_by,
            limit=limit,
            cursor=cursor,
            include_meta=include_meta,
        )


class PanelAccess:
    """访问面 · 时序查询（仅直连；``asof_join`` 为本地计算，两模式可用）。"""

    def __init__(self, platform: FinDataPlatform) -> None:
        self._platform = platform

    def get_series(
        self,
        dataset: str,
        *,
        fields: Sequence[str],
        start: date,
        end: date,
        as_of: datetime,
        entities: Sequence[int] | None = None,
        freq: str = "1d",
        fill: str = "none",
        calendar: str | None = "trading",
        adjust: str | None = None,
        agg: Mapping[str, str] | None = None,
        rolling: Rolling | None = None,
        change: int | None = None,
    ) -> SdkResult:
        return self._platform._call(
            "panel_series",
            dataset,
            fields=fields,
            start=start,
            end=end,
            as_of=as_of,
            entities=entities,
            freq=freq,
            fill=fill,
            calendar=calendar,
            adjust=adjust,
            agg=agg,
            rolling=rolling,
            change=change,
        )

    def get_panel(
        self,
        dataset: str,
        *,
        fields: Sequence[str],
        start: date,
        end: date,
        as_of: datetime,
        entities: Sequence[int] | None = None,
        freq: str = "1d",
        fill: str = "none",
        calendar: str | None = "trading",
        adjust: str | None = None,
        agg: Mapping[str, str] | None = None,
        rolling: Rolling | None = None,
        change: int | None = None,
        shape: str = "wide",
    ) -> SdkResult:
        return self._platform._call(
            "panel_panel",
            dataset,
            fields=fields,
            start=start,
            end=end,
            as_of=as_of,
            entities=entities,
            freq=freq,
            fill=fill,
            calendar=calendar,
            adjust=adjust,
            agg=agg,
            rolling=rolling,
            change=change,
            shape=shape,
        )

    def get_cross_section(
        self,
        dataset: str,
        *,
        date: date,  # noqa: A002 - 与 panel 契约参数名一致
        as_of: datetime,
        fields: Sequence[str],
        entities: Sequence[int] | None = None,
        calendar: str | None = None,
        adjust: str | None = None,
    ) -> SdkResult:
        return self._platform._call(
            "panel_cross_section",
            dataset,
            day=date,
            as_of=as_of,
            fields=fields,
            entities=entities,
            calendar=calendar,
            adjust=adjust,
        )

    def get_versions(
        self,
        dataset: str,
        *,
        entities: Sequence[int] | None = None,
        start: date | None = None,
        end: date | None = None,
        fields: Sequence[str] | None = None,
        as_of: datetime | None = None,
        mode: str = "history",
    ) -> SdkResult:
        return self._platform._call(
            "panel_versions",
            dataset,
            entities=entities,
            start=start,
            end=end,
            fields=fields,
            as_of=as_of,
            mode=mode,
        )

    def asof_join(
        self,
        left: pd.DataFrame,
        right: pd.DataFrame,
        *,
        left_on: str,
        right_on: str | None = None,
        by: str | Sequence[str] | None = None,
        direction: str = "backward",
        tolerance: Any = None,
        suffixes: tuple[str, str] = ("", "_right"),
    ) -> pd.DataFrame:
        """asof join（本地计算；PIT 安全默认 ``backward``）。"""
        from fin_data_platform.panel import asof_join as _asof_join

        return _asof_join(
            left,
            right,
            left_on=left_on,
            right_on=right_on,
            by=by,
            direction=direction,  # type: ignore[arg-type]
            tolerance=tolerance,
            suffixes=suffixes,
        )


class ControlIntent:
    """控制面意图：回填 / 物化 / 全局任务（只提交意图，平台执行）。"""

    def __init__(self, platform: FinDataPlatform) -> None:
        self._platform = platform

    def ensure(
        self,
        dataset: str,
        *,
        codes: Sequence[str] | None = None,
        window: tuple[date, date] | None = None,
        request_id: str | None = None,
    ) -> SdkRun:
        return self._platform._call(
            "control_ensure", dataset, codes=codes, window=window, request_id=request_id
        )

    def materialize(
        self,
        factor: str,
        *,
        dataset: str | None = None,
        request_id: str | None = None,
    ) -> SdkRun:
        return self._platform._call(
            "control_materialize", factor, dataset=dataset, request_id=request_id
        )

    def trigger(
        self,
        job_id: str,
        *,
        window: tuple[date, date] | None = None,
        request_id: str | None = None,
    ) -> SdkRun:
        return self._platform._call(
            "control_trigger", job_id, window=window, request_id=request_id
        )
