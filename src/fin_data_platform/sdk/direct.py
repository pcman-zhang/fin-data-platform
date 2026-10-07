"""SDK 直连后端：复用平台内核（access / FactorAPI / query / panel / ControlClient）。

- 数据读取走只读 DSN（建议只读角色）；控制面意图需 ``control_dsn``（``meta`` 写权限）；
- 连接时校验 schema 修订（``compat.check_schema_revision``）；
- 各层结构化异常统一映射为 :class:`FinDataError`（保持 code / hint）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, text

from fin_data_platform.access import read as access_read
from fin_data_platform.access.reader import normalize_as_of
from fin_data_platform.control import ControlClient
from fin_data_platform.derived.factor_api import FactorAPI
from fin_data_platform.derived.store import SqlAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.panel import (
    Rolling,
    get_cross_section,
    get_panel,
    get_series,
    get_versions,
)
from fin_data_platform.query import FilterClause, RowsQuery, read_rows
from fin_data_platform.runtime.repository import SqlMetaRepository
from fin_data_platform.sdk.compat import check_schema_revision
from fin_data_platform.sdk.config import SdkConfig
from fin_data_platform.sdk.errors import FinDataError, from_backend_error
from fin_data_platform.sdk.models import (
    ControlRunInfo,
    FactorResultMeta,
    ResultMeta,
    SdkResult,
    SdkRun,
)


class DirectBackend:
    """直连后端（engine 可注入，供测试与嵌入式使用）。"""

    def __init__(
        self,
        config: SdkConfig,
        *,
        engine: Engine | None = None,
        control_engine: Engine | None = None,
        specs: Mapping[str, DatasetSpec] | None = None,
    ) -> None:
        self._config = config
        self._specs = dict(specs) if specs is not None else load_all()
        self._owned_engine = engine is None
        self._owned_control_engine = control_engine is None
        self._engine = engine if engine is not None else _engine_from_dsn(config.dsn)
        self._control_engine = (
            control_engine
            if control_engine is not None
            else (_engine_from_dsn(config.control_dsn) if config.control_dsn else None)
        )
        self._factors = FactorAPI(self._engine, specs=self._specs)
        self._control: ControlClient | None = None

    def close(self) -> None:
        """释放自建数据库引擎（注入的引擎由调用方管理）。"""
        if self._owned_engine:
            self._engine.dispose()
        if self._owned_control_engine and self._control_engine is not None:
            self._control_engine.dispose()

    # ------------------------------------------------------------ 连接校验
    def check(self) -> None:
        try:
            with self._engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        except Exception as exc:
            raise FinDataError(
                "upstream_unavailable",
                f"数据库连接 / 修订读取失败：{exc}",
                hint="检查 DSN 与数据库可用性（alembic upgrade head）",
            ) from exc
        check_schema_revision(revision)

    # ------------------------------------------------------------ 读取
    def raw_read(
        self,
        dataset: str,
        *,
        fields: Sequence[str] | None,
        entities: Sequence[int] | None,
        window: tuple[date, date] | None,
        adjust: str | None,
        as_of: datetime,
        align_calendar: bool,
        limit: int | None = None,
    ) -> SdkResult:
        try:
            result = access_read(
                self._engine,
                dataset,
                list(fields) if fields else None,
                as_of=normalize_as_of(as_of),
                adjust=adjust,
                entities=list(entities) if entities else None,
                window=window,
                align_calendar=align_calendar,
                specs=self._specs,
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        frame = result.table.to_pandas()
        warnings = list(result.meta.warnings)
        if limit is not None and len(frame) > limit:
            frame = frame.head(limit)
            warnings.append(f"结果超过 limit（{limit}），已截断")
        meta = ResultMeta(
            dataset=result.meta.dataset,
            as_of=result.meta.as_of,
            adjust=result.meta.adjust,
            semantic_version=result.meta.semantic_version,
            row_count=len(frame),
            warnings=warnings,
            aligned=result.meta.aligned,
            calendar_dataset=result.meta.calendar_dataset,
            status_dataset=result.meta.status_dataset,
            trading_days=result.meta.trading_days,
            factor_dataset=result.meta.factor_dataset,
            adjusted_fields=list(result.meta.adjusted_fields),
        )
        return SdkResult(frame=frame, table=result.table, meta=meta)

    def factor_read(
        self,
        output: str,
        *,
        dataset: str | None,
        entities: Sequence[int] | None,
        window: tuple[date, date] | None,
        as_of: datetime,
        algorithm_id: str | None,
        limit: int | None = None,
    ) -> SdkResult:
        try:
            result = self._factors.read(
                output,
                as_of=normalize_as_of(as_of),
                dataset=dataset,
                algorithm_id=algorithm_id,
                entities=list(entities) if entities else None,
                window=window,
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        frame = result.values.to_pandas()
        warnings: list[str] = []
        if limit is not None and len(frame) > limit:
            frame = frame.head(limit)
            warnings.append(f"结果超过 limit（{limit}），已截断")
        meta = FactorResultMeta(
            dataset=result.meta.dataset,
            output=result.meta.output,
            algorithm_id=result.meta.algorithm_id,
            algorithm_version=result.meta.algorithm_version,
            as_of=result.meta.as_of,
            materialized=result.meta.materialized,
            data_generation=result.meta.data_generation,
            computed_at=result.meta.computed_at,
            upstream_fingerprint=result.meta.upstream_fingerprint,
            row_count=len(frame),
            warnings=warnings,
        )
        return SdkResult(frame=frame, table=result.values, meta=meta)

    def read_model(
        self,
        dataset: str,
        *,
        version_mode: str,
        as_of: datetime | None,
        as_of_policy: str,
        fallback_mode: str,
        entities: Sequence[int] | None,
        window: tuple[date, date] | None,
        fields: Sequence[str] | None,
        filters: Sequence[Mapping[str, Any]] | None,
        order_by: Sequence[str] | None,
        limit: int,
        cursor: str | None,
        include_meta: bool,
    ) -> SdkResult:
        query = RowsQuery(
            dataset=dataset,
            version_mode=version_mode,
            as_of=as_of,
            as_of_policy=as_of_policy,
            fallback_mode=fallback_mode,
            entities=tuple(entities or ()),
            window=window,
            fields=tuple(fields or ()),
            filters=tuple(_clause(item) for item in (filters or ())),
            order_by=tuple(order_by or ()),
            limit=limit,
            cursor=cursor,
            include_meta=include_meta,
        )
        try:
            result = read_rows(self._engine, query, specs=self._specs)
        except Exception as exc:
            raise from_backend_error(exc) from exc
        meta = ResultMeta(
            dataset=result.meta.dataset,
            as_of=result.meta.as_of,
            version_mode=result.meta.version_mode,
            policy=result.meta.policy,
            fallback=result.meta.fallback,
            semantic_version=result.meta.semantic_version,
            data_generation=result.meta.data_generation,
            row_count=result.meta.row_count,
            warnings=list(result.meta.warnings),
            next_cursor=result.meta.next_cursor,
        )
        return SdkResult(frame=result.frame, meta=meta)

    # ------------------------------------------------------------ 时序查询（panel；仅直连）
    def panel_series(
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
        try:
            frame = get_series(
                self._engine,
                dataset,
                fields=fields,
                start=start,
                end=end,
                as_of=normalize_as_of(as_of),
                entities=entities,
                freq=freq,
                fill=fill,
                calendar=calendar,
                adjust=adjust,
                agg=agg,
                rolling=rolling,
                change=change,
                specs=self._specs,
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        return SdkResult(frame=frame, meta=self._panel_meta(dataset, as_of, len(frame)))

    def panel_panel(
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
        try:
            frame = get_panel(
                self._engine,
                dataset,
                fields=fields,
                start=start,
                end=end,
                as_of=normalize_as_of(as_of),
                entities=entities,
                freq=freq,
                fill=fill,
                calendar=calendar,
                adjust=adjust,
                agg=agg,
                rolling=rolling,
                change=change,
                shape=shape,
                specs=self._specs,
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        return SdkResult(frame=frame, meta=self._panel_meta(dataset, as_of, len(frame)))

    def panel_cross_section(
        self,
        dataset: str,
        *,
        day: date,
        as_of: datetime,
        fields: Sequence[str],
        entities: Sequence[int] | None = None,
        calendar: str | None = None,
        adjust: str | None = None,
    ) -> SdkResult:
        try:
            frame = get_cross_section(
                self._engine,
                dataset,
                date=day,
                as_of=normalize_as_of(as_of),
                fields=fields,
                entities=entities,
                calendar=calendar,
                adjust=adjust,
                specs=self._specs,
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        return SdkResult(frame=frame, meta=self._panel_meta(dataset, as_of, len(frame)))

    def panel_versions(
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
        try:
            frame = get_versions(
                self._engine,
                dataset,
                entities=entities,
                start=start,
                end=end,
                fields=fields,
                as_of=normalize_as_of(as_of) if as_of is not None else None,
                mode=mode,
                specs=self._specs,
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        return SdkResult(frame=frame, meta=self._panel_meta(dataset, as_of, len(frame)))

    def _panel_meta(self, dataset: str, as_of: datetime | None, row_count: int) -> ResultMeta:
        spec = self._specs.get(dataset)
        return ResultMeta(
            dataset=dataset,
            as_of=normalize_as_of(as_of) if as_of is not None else None,
            semantic_version=spec.semantic_version if spec is not None else 0,
            row_count=row_count,
        )

    # ------------------------------------------------------------ 控制面意图
    def _control_client(self) -> ControlClient:
        if self._control is None:
            if self._control_engine is None:
                raise FinDataError(
                    "control_unavailable",
                    "直连模式提交控制面意图需要 control_dsn（meta 写权限）",
                    hint="设置 FDP_SDK_CONTROL_DSN，或改用 REST 模式（FDP_SDK_MODE=rest）",
                )
            self._control = ControlClient(
                self._control_engine,
                specs=self._specs,
                meta=SqlMetaRepository(self._control_engine),
                algorithms=SqlAlgorithmStore(self._control_engine),
            )
        return self._control

    def control_ensure(
        self,
        dataset: str,
        *,
        codes: Sequence[str] | None,
        window: tuple[date, date] | None,
        request_id: str | None,
    ) -> SdkRun:
        control = self._control_client()  # 无 control_dsn 时明确报错
        targets = list(codes) if codes is not None else self._registered_sync_codes(dataset)
        if not targets:
            raise FinDataError(
                "job_not_registered",
                f"未注册同步任务：{dataset}",
                hint="检查数据集名与同步装配（FDP_SYNC_CODES）",
            )
        if len(targets) != 1:
            # 提交前拒绝（避免多代码意图已入队后才报错）
            raise FinDataError(
                "invalid_request",
                f"ensure 需单代码提交（收到 {len(targets)} 个）",
                hint="显式指定 codes 并分别提交",
            )
        try:
            runs = control.ensure(
                dataset, codes=targets, window=window, request_id=request_id
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        handles = list(runs)
        if not handles:
            raise FinDataError(
                "invalid_request",
                "窗口为空（水位已追平）",
                hint="显式提供 window 或等待新数据后再提交",
            )
        return self._run(handles[0])

    def _registered_sync_codes(self, dataset: str) -> list[str]:
        """该数据集已注册的同步代码（提交前校验用；未装配 control 时为空）。"""
        if self._control_engine is None:
            return []
        prefix = f"sync.{dataset}."
        meta = SqlMetaRepository(self._control_engine)
        return sorted(
            item.job_id[len(prefix) :]
            for item in meta.list_defs()
            if item.job_id.startswith(prefix)
        )

    def control_materialize(
        self, factor: str, *, dataset: str | None, request_id: str | None
    ) -> SdkRun:
        try:
            handle = self._control_client().materialize(
                factor, dataset=dataset, request_id=request_id
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        return self._run(handle)

    def control_trigger(
        self,
        job_id: str,
        *,
        window: tuple[date, date] | None,
        request_id: str | None,
    ) -> SdkRun:
        try:
            handle = self._control_client().trigger(
                job_id, window=window, request_id=request_id
            )
        except Exception as exc:
            raise from_backend_error(exc) from exc
        return self._run(handle)

    def _run(self, handle: Any) -> SdkRun:
        info = ControlRunInfo(
            run_id=handle.run_id,
            job_id=handle.job_id,
            dataset=handle.dataset,
            kind=handle.kind,
            status=handle.status,
            window_start=handle.window_start,
            window_end=handle.window_end,
            created=handle.created,
            note=(
                None
                if handle.created
                else ("命中既有运行（幂等）" if handle.matched_via else "命中既有运行")
            ),
        )

        def _wait(timeout: float | None) -> ControlRunInfo:
            try:
                run = handle.wait(timeout=timeout)
            except Exception as exc:
                raise from_backend_error(exc) from exc
            return ControlRunInfo(
                run_id=run.run_id,
                job_id=run.job_id,
                dataset=run.dataset,
                kind=run.kind,
                status=run.status,
                window_start=run.window_start,
                window_end=run.window_end,
                created=info.created,
                note=info.note,
            )

        return SdkRun(info=info, _wait=_wait)


def _engine_from_dsn(dsn: str | None) -> Engine:
    if not dsn:
        raise FinDataError(
            "invalid_config", "缺少 DSN", hint="设置 FDP_SDK_DSN（直连只读 DSN）"
        )
    return create_engine(dsn, pool_pre_ping=True)


def _clause(item: Mapping[str, Any]) -> FilterClause:
    if not isinstance(item, Mapping) or "field" not in item or "op" not in item:
        raise FinDataError(
            "unsupported_filter",
            "filters 条目需含 field / op / value",
            hint=(
                '形如 [{"field":"trade_date","op":"between",'
                '"value":["2024-01-01","2024-12-31"]}]'
            ),
        )
    return FilterClause(
        field=str(item["field"]), op=str(item["op"]), value=item.get("value")
    )
