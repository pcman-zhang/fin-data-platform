"""SDK REST 后端（httpx）：调 ``/v1`` 数据面与控制面；错误解析 RFC 9457。

- 与直连模式同签名、同结果模型（仅改配置即可切换，doc-21 §5）；
- 首期无 API Key（延续 TASK-3.7 决策）；``panel`` 暂不支持（直连可用）；
- ``ensure`` 需显式 ``codes``（REST 端点要求；直连可在单一装配下省略）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import httpx
import pandas as pd

from fin_data_platform.sdk.compat import check_schema_revision
from fin_data_platform.sdk.config import SdkConfig
from fin_data_platform.sdk.errors import FinDataError, from_problem
from fin_data_platform.sdk.models import (
    ControlRunInfo,
    FactorResultMeta,
    ResultMeta,
    SdkResult,
    SdkRun,
)

_TERMINAL = frozenset({"succeeded", "failed", "dead", "cancelled", "interrupted"})
_PANEL_UNSUPPORTED = (
    "REST 模式暂不支持时序查询（panel）；请使用直连模式（FDP_SDK_MODE=direct）"
)


class RestBackend:
    """REST 后端（``client`` 可注入，供测试）。"""

    def __init__(self, config: SdkConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(
            base_url=config.rest_url, timeout=config.timeout
        )
        self._owned_client = client is None
        self._columns: dict[str, list[str]] = {}

    def close(self) -> None:
        """关闭自建连接池（注入的 client 由调用方管理）。"""
        if self._owned_client:
            self._client.close()

    # ------------------------------------------------------------ 连接校验
    def check(self) -> None:
        payload = self._request("GET", "/v1/health")
        if not payload.get("ok"):
            errors = "; ".join(payload.get("errors") or []) or "检查平台服务"
            raise FinDataError("upstream_unavailable", "平台健康检查未通过", hint=errors)
        revision = payload.get("schema_revision")
        if revision:
            # SDK 侧兼容区间校验（与直连同一实现）
            check_schema_revision(str(revision))
        elif not (payload.get("checks") or {}).get("schema_revision", False):
            raise FinDataError(
                "schema_incompatible",
                "平台 schema 修订校验未通过",
                hint="升级平台（alembic upgrade head）",
            )

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
        params = _base_params(
            as_of=_normalize_as_of(as_of),
            entities=entities,
            window=window,
            fields=fields,
        )
        if adjust:
            params.append(("adjust", adjust))
        if align_calendar:
            params.append(("align_calendar", "true"))
        if limit is not None:
            params.append(("limit", str(limit)))
        payload = self._request("GET", f"/v1/raw/{dataset}/rows", params=params)
        return self._result(payload, dataset=dataset, fields=fields)

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
        params: list[tuple[str, str]] = [("as_of", _normalize_as_of(as_of).isoformat())]
        if dataset:
            params.append(("dataset", dataset))
        if algorithm_id:
            params.append(("algorithm_id", algorithm_id))
        if limit is not None:
            params.append(("limit", str(limit)))
        _append_scope(params, entities=entities, window=window, fields=None)
        payload = self._request("GET", f"/v1/factors/{output}/rows", params=params)
        frame = self._frame(payload, fields=None, dataset=dataset)
        meta_payload = payload.get("meta") or {}
        meta = FactorResultMeta(
            dataset=str(meta_payload.get("dataset") or dataset or ""),
            output=str(meta_payload.get("output") or output),
            algorithm_id=str(meta_payload.get("algorithm_id") or ""),
            algorithm_version=meta_payload.get("algorithm_version"),
            as_of=_parse_dt(meta_payload.get("as_of")) or as_of,
            materialized=bool(meta_payload.get("materialized", False)),
            data_generation=meta_payload.get("data_generation"),
            computed_at=_parse_dt(meta_payload.get("computed_at")),
            upstream_fingerprint=meta_payload.get("upstream_fingerprint"),
            row_count=int(meta_payload.get("row_count") or len(frame)),
            warnings=list(meta_payload.get("warnings") or []),
        )
        return SdkResult(frame=frame, meta=meta)

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
        params: list[tuple[str, str]] = [
            ("version_mode", version_mode),
            ("as_of_policy", as_of_policy),
            ("fallback_mode", fallback_mode),
            ("limit", str(limit)),
            ("include_meta", "true" if include_meta else "false"),
        ]
        if as_of is not None:
            params.append(("as_of", _normalize_as_of(as_of).isoformat()))
        if cursor:
            params.append(("cursor", cursor))
        if order_by:
            params.append(("order_by", ",".join(order_by)))
        if filters:
            params.append(("filters", json.dumps(list(filters), ensure_ascii=False)))
        _append_scope(params, entities=entities, window=window, fields=fields)
        payload = self._request("GET", f"/v1/datasets/{dataset}/rows", params=params)
        return self._result(payload, dataset=dataset, fields=fields)

    # ------------------------------------------------------------ 时序查询（REST 暂不支持）
    def panel_series(self, *args: Any, **kwargs: Any) -> SdkResult:
        raise FinDataError("unsupported_in_rest_mode", _PANEL_UNSUPPORTED)

    def panel_panel(self, *args: Any, **kwargs: Any) -> SdkResult:
        raise FinDataError("unsupported_in_rest_mode", _PANEL_UNSUPPORTED)

    def panel_cross_section(self, *args: Any, **kwargs: Any) -> SdkResult:
        raise FinDataError("unsupported_in_rest_mode", _PANEL_UNSUPPORTED)

    def panel_versions(self, *args: Any, **kwargs: Any) -> SdkResult:
        raise FinDataError("unsupported_in_rest_mode", _PANEL_UNSUPPORTED)

    # ------------------------------------------------------------ 控制面意图
    def control_ensure(
        self,
        dataset: str,
        *,
        codes: Sequence[str] | None,
        window: tuple[date, date] | None,
        request_id: str | None,
    ) -> SdkRun:
        if not codes:
            raise FinDataError(
                "invalid_request",
                "REST 模式 ensure 需显式 codes",
                hint="直连模式可在单一装配下省略 code；REST 端点要求代码清单",
            )
        if len(codes) != 1:
            raise FinDataError(
                "invalid_request",
                f"REST 模式 ensure 需单代码提交（收到 {len(codes)} 个）",
                hint="多代码请分别提交",
            )
        code = str(codes[0])
        job_id = f"sync.{dataset}.{code}"
        defs = self._request("GET", "/v1/jobs/defs")
        if not any(str(item.get("job_id")) == job_id for item in defs):
            raise FinDataError(
                "job_not_registered",
                f"任务未注册：{job_id}",
                hint="检查代码拼写或调度装配（GET /v1/jobs/defs）",
            )
        body: dict[str, Any] = {
            "dataset": dataset,
            "codes": [code],
            "request_id": request_id,
        }
        if window is not None:
            body["start"], body["end"] = window[0].isoformat(), window[1].isoformat()
        payload = self._request("POST", "/v1/jobs/sync", payload=body)
        items = [*(payload.get("submitted") or []), *(payload.get("skipped") or [])]
        if len(items) != 1:
            raise FinDataError(
                "invalid_request",
                f"REST 返回 {len(items)} 个运行；SDK 需单代码提交",
                hint="请显式指定单个 code 并分别提交",
            )
        item = items[0]
        note = str(item.get("note") or "") or None
        if item.get("status") == "skipped" and note and "窗口重复" not in note:
            if "未注册" in note:
                raise FinDataError(
                    "job_not_registered", note, hint="检查代码拼写或调度装配"
                )
            if "窗口" in note:
                raise FinDataError(
                    "invalid_window", note, hint="窗口缺省 = 水位+1 ~ 最近已收盘交易日"
                )
            raise FinDataError("invalid_request", note)
        info = ControlRunInfo(
            run_id=int(item.get("run_id") or 0),
            job_id=str(item.get("job_id") or job_id),
            dataset=dataset,
            kind="sync",
            status=str(item.get("status") or ""),
            window_start=_parse_date(item.get("window_start")),
            window_end=_parse_date(item.get("window_end")),
            created=note is None,
            note=note,
        )
        return SdkRun(
            info=info,
            _wait=lambda timeout: self._wait_run(
                info.run_id, timeout, created=info.created, note=info.note
            ),
        )

    def control_materialize(
        self, factor: str, *, dataset: str | None, request_id: str | None
    ) -> SdkRun:
        body: dict[str, Any] = {"factor": factor, "request_id": request_id}
        if dataset:
            body["dataset"] = dataset
        payload = self._request("POST", "/v1/jobs/materialize", payload=body)
        info = ControlRunInfo(
            run_id=int(payload.get("run_id") or 0),
            job_id=str(payload.get("job_id") or ""),
            dataset=str(payload.get("dataset") or dataset or ""),
            kind="derive",
            status=str(payload.get("status") or ""),
            window_start=_parse_date(payload.get("window_start")),
            window_end=_parse_date(payload.get("window_end")),
            created=bool(payload.get("created", True)),
            note=payload.get("note"),
        )
        return SdkRun(
            info=info,
            _wait=lambda timeout: self._wait_run(
                info.run_id, timeout, created=info.created, note=info.note
            ),
        )

    def control_trigger(
        self,
        job_id: str,
        *,
        window: tuple[date, date] | None,
        request_id: str | None,
    ) -> SdkRun:
        if window is not None:
            raise FinDataError(
                "unsupported_in_rest_mode",
                "REST 模式暂不支持显式窗口（trigger 窗口 = 触发日）",
                hint="改用直连模式，或省略 window",
            )
        body: dict[str, Any] = {"job_id": job_id, "request_id": request_id}
        payload = self._request("POST", "/v1/jobs/trigger", payload=body)
        info = ControlRunInfo(
            run_id=int(payload.get("run_id") or 0),
            job_id=str(payload.get("job_id") or job_id),
            dataset="",
            kind="",
            status=str(payload.get("status") or ""),
            window_start=_parse_date(payload.get("window_start")),
            window_end=_parse_date(payload.get("window_end")),
            created=bool(payload.get("created", True)),
            note=payload.get("note"),
        )
        return SdkRun(
            info=info,
            _wait=lambda timeout: self._wait_run(
                info.run_id, timeout, created=info.created, note=info.note
            ),
        )

    def _wait_run(
        self,
        run_id: int,
        timeout: float | None,
        *,
        created: bool = True,
        note: str | None = None,
    ) -> ControlRunInfo:
        if run_id <= 0:
            raise FinDataError(
                "invalid_request", "运行 ID 无效（无可等待的运行）", hint="检查提交结果"
            )
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            payload = self._request("GET", f"/v1/jobs/{run_id}")
            status = str(payload.get("status") or "")
            if status in _TERMINAL:
                return ControlRunInfo(
                    run_id=int(payload.get("run_id") or run_id),
                    job_id=str(payload.get("job_id") or ""),
                    dataset=str(payload.get("dataset") or ""),
                    kind=str(payload.get("kind") or ""),
                    status=status,
                    window_start=_parse_date(payload.get("window_start")),
                    window_end=_parse_date(payload.get("window_end")),
                    created=created,
                    note=note,
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise FinDataError(
                    "timeout",
                    f"等待运行 {run_id} 超时（任务继续在平台侧执行）",
                    hint=f"稍后用 GET /v1/jobs/{run_id} 查询",
                )
            time.sleep(0.2)

    # ------------------------------------------------------------ 帧与元数据
    def _frame(
        self, payload: Mapping[str, Any], *, fields: Sequence[str] | None, dataset: str | None
    ) -> pd.DataFrame:
        """响应体 → DataFrame（空结果保留列集合：meta.columns / 请求 fields / 字典 schema）。"""
        rows = payload.get("rows") or []
        if rows:
            return pd.DataFrame(rows)
        meta_payload = payload.get("meta") or {}
        columns = list(meta_payload.get("columns") or [])
        if not columns and fields:
            columns = list(fields)
        if not columns and dataset:
            columns = self._dataset_columns(dataset)
        return pd.DataFrame(columns=columns)

    def _dataset_columns(self, dataset: str) -> list[str]:
        cached = self._columns.get(dataset)
        if cached is not None:
            return cached
        try:
            payload = self._request("GET", f"/v1/datasets/{dataset}/schema")
            columns = list((payload.get("properties") or {}).keys())
        except FinDataError:
            columns = []
        self._columns[dataset] = columns
        return columns

    def _result(
        self,
        payload: Mapping[str, Any],
        *,
        dataset: str,
        fields: Sequence[str] | None = None,
    ) -> SdkResult:
        frame = self._frame(payload, fields=fields, dataset=dataset)
        meta_payload = dict(payload.get("meta") or {})
        meta = ResultMeta(
            dataset=str(meta_payload.get("dataset") or dataset),
            as_of=_parse_dt(meta_payload.get("as_of")),
            version_mode=meta_payload.get("version_mode"),
            policy=meta_payload.get("policy"),
            fallback=meta_payload.get("fallback"),
            adjust=meta_payload.get("adjust"),
            semantic_version=int(meta_payload.get("semantic_version") or 0),
            data_generation=meta_payload.get("data_generation"),
            row_count=int(meta_payload.get("row_count") or len(frame)),
            warnings=list(meta_payload.get("warnings") or []),
            aligned=meta_payload.get("aligned"),
            calendar_dataset=meta_payload.get("calendar_dataset"),
            status_dataset=meta_payload.get("status_dataset"),
            trading_days=meta_payload.get("trading_days"),
            factor_dataset=meta_payload.get("factor_dataset"),
            adjusted_fields=list(meta_payload.get("adjusted_fields") or []),
            next_cursor=meta_payload.get("next_cursor"),
        )
        return SdkResult(frame=frame, meta=meta)

    # ------------------------------------------------------------ HTTP
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        payload: Any = None,
    ) -> Any:
        try:
            response = self._client.request(method, path, params=params, json=payload)
        except httpx.HTTPError as exc:
            raise FinDataError(
                "upstream_unavailable",
                f"REST 请求失败：{type(exc).__name__}: {exc}",
                hint=f"检查后端可达性（{self._config.rest_url}）",
            ) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"detail": response.text[:200]}
            if isinstance(body, dict) and "title" in body:
                raise from_problem(body, status=response.status_code)
            detail = str(body.get("detail") if isinstance(body, dict) else body)
            raise _fallback_error(response.status_code, detail)
        try:
            return response.json()
        except ValueError as exc:
            raise FinDataError(
                "upstream_unavailable",
                "REST 响应不是合法 JSON",
                hint=f"检查后端版本与端点：{path}",
            ) from exc


def _fallback_error(status: int, detail: str) -> FinDataError:
    """非问题体错误（管理端点）→ 结构化 code（与直连模式语义对齐）。"""
    if "水位已追平" in detail:
        return FinDataError(
            "invalid_request",
            detail,
            hint="窗口为空；显式提供 window 或等待新数据后再提交",
        )
    if "未注册" in detail:
        return FinDataError(
            "job_not_registered", detail, hint="检查任务标识与 Runtime 装配"
        )
    if "ensure" in detail:
        return FinDataError("invalid_request", detail, hint="按代码任务请使用 control.ensure")
    if "materialize" in detail:
        return FinDataError(
            "invalid_request", detail, hint="物化任务请使用 control.materialize"
        )
    if status == 422 and "窗口" in detail:
        return FinDataError("invalid_window", detail, hint="检查窗口与最近已收盘交易日")
    if status == 422:
        return FinDataError("invalid_request", detail)
    if status == 404:
        return FinDataError("not_found", detail)
    return FinDataError(f"http_{status}", detail)


def _base_params(
    *,
    as_of: datetime,
    entities: Sequence[int] | None,
    window: tuple[date, date] | None,
    fields: Sequence[str] | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("as_of", as_of.isoformat())]
    _append_scope(params, entities=entities, window=window, fields=fields)
    return params


def _append_scope(
    params: list[tuple[str, str]],
    *,
    entities: Sequence[int] | None,
    window: tuple[date, date] | None,
    fields: Sequence[str] | None,
) -> None:
    for entity_id in entities or ():
        params.append(("entity_id", str(entity_id)))
    if window is not None:
        params.append(("start", window[0].isoformat()))
        params.append(("end", window[1].isoformat()))
    if fields:
        params.append(("fields", ",".join(fields)))


def _normalize_as_of(value: datetime) -> datetime:
    """aware → naive UTC（与直连模式同口径；naive 视为 UTC）。"""
    if value.tzinfo is None:
        return value
    from datetime import UTC

    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
