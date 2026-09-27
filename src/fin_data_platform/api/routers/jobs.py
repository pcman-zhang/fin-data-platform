"""任务与水位：运行记录查询 + 控制面意图提交（写 ``meta`` 队列，Runtime 执行）。

控制面数据使用写连接（平台内部写入端）；同步/物化触发不直接调用采集/计算，
仅提交意图（``ControlClient``，与库接口同一实现），由 scheduler/worker 认领
（避免绕过控制面）。窗口缺省为「水位+1 ~ 最近已收盘交易日」（落库日历）；
显式终点晚于最近已收盘 → 422（防止水位被顶到未来导致同步停摆，TASK-3.26）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.schemas import (
    JobRunOut,
    MaterializeRequest,
    MaterializeResponse,
    SyncItem,
    SyncRequest,
    SyncResponse,
    WatermarkOut,
)
from fin_data_platform.control import (
    ControlClient,
    InvalidWindow,
    JobNotRegistered,
    UnknownFactor,
)
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.models import JobStatus

router = APIRouter(tags=["jobs"])

Context = Annotated[ApiContext, Depends(get_context)]

_STATUS_PATTERN = "|".join(status.value for status in JobStatus)


def _control(context: ApiContext) -> ControlClient:
    """控制面意图客户端（平台内部写入端；与库接口同一实现）。"""
    return ControlClient(
        context.writer_engine,
        specs=context.specs,
        meta=context.meta,
        algorithms=context.algorithms,
    )


@router.get("/jobs", response_model=list[JobRunOut], summary="任务运行记录")
def list_jobs(
    context: Context,
    status: Annotated[str | None, Query(pattern=f"^({_STATUS_PATTERN})$")] = None,
    job_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[JobRunOut]:
    runs = context.meta.list_runs(status=status, job_id=job_id, limit=limit)
    return [JobRunOut.from_run(run) for run in runs]


@router.get("/jobs/{run_id}", response_model=JobRunOut, summary="任务详情")
def get_job(run_id: int, context: Context) -> JobRunOut:
    run = context.meta.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"任务运行不存在: {run_id}")
    return JobRunOut.from_run(run)


@router.get("/watermarks", response_model=list[WatermarkOut], summary="数据水位")
def list_watermarks(context: Context) -> list[WatermarkOut]:
    return [
        WatermarkOut.from_watermark(mark) for mark in context.meta.list_watermarks()
    ]


@router.post(
    "/jobs/sync",
    response_model=SyncResponse,
    status_code=202,
    summary="触发同步（提交意图；WebUI 侧二次确认）",
)
def trigger_sync(payload: SyncRequest, context: Context) -> SyncResponse:
    codes = list(dict.fromkeys(payload.codes))
    if payload.request_id is not None and len(codes) > 1:
        # 幂等键只能对应一次运行：多代码请分别提交（否则后续代码会被静默归属到首个运行）
        raise HTTPException(
            status_code=422,
            detail="request_id 仅支持单代码提交；多代码请分别提交或省略 request_id",
        )
    control = _control(context)
    last_closed = control.last_closed() or utcnow().date()
    end = payload.end or last_closed
    if end > last_closed:
        raise HTTPException(
            status_code=422,
            detail=f"窗口终点 {end} 晚于最近已收盘交易日 {last_closed}；收盘发布后重试",
        )
    submitted: list[SyncItem] = []
    skipped: list[SyncItem] = []

    for code in codes:
        job_id = f"sync.{payload.dataset}.{code}"
        item = SyncItem(code=code, job_id=job_id, status="skipped")
        window = (payload.start, end) if payload.start is not None else None
        try:
            runs = control.ensure(
                payload.dataset,
                codes=[code],
                window=window,
                request_id=payload.request_id,
                priority=payload.priority,
            )
        except JobNotRegistered:
            item.note = "任务未注册（检查代码拼写或调度配置）"
            skipped.append(item)
            continue
        except InvalidWindow as exc:
            item.window_start, item.window_end = payload.start, end
            item.note = f"窗口无效：{exc.detail}"
            skipped.append(item)
            continue
        if not runs:  # 水位已追平：无窗口可提交
            item.note = "窗口为空（水位已追平）"
            skipped.append(item)
            continue

        handle = runs.runs[0]
        item.run_id = handle.run_id
        item.status = handle.status
        item.window_start, item.window_end = handle.window_start, handle.window_end
        if handle.created:
            submitted.append(item)
        elif handle.matched_via == "request_id":
            item.note = "幂等键命中（返回既有运行）"
            submitted.append(item)
        else:
            item.note = "窗口重复（同窗口意图已存在，幂等忽略）"
            skipped.append(item)

    return SyncResponse(submitted=submitted, skipped=skipped)


@router.post(
    "/jobs/materialize",
    response_model=MaterializeResponse,
    status_code=202,
    summary="触发物化（提交意图；幂等返回既有运行）",
)
def trigger_materialize(payload: MaterializeRequest, context: Context) -> MaterializeResponse:
    control = _control(context)
    try:
        handle = control.materialize(
            payload.factor,
            dataset=payload.dataset,
            request_id=payload.request_id,
            priority=payload.priority,
        )
    except UnknownFactor as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobNotRegistered as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MaterializeResponse(
        run_id=handle.run_id,
        job_id=handle.job_id,
        dataset=handle.dataset,
        output=handle.job_id.rpartition(".")[2],
        status=handle.status,
        window_start=handle.window_start,
        window_end=handle.window_end,
        version_dimension=handle.version_dimension,
        created=handle.created,
        note=None if handle.created else "命中既有运行（幂等）",
    )
