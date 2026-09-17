"""派生算法登记与升级台账（只读；doc-10 §3.5、doc-14 #7）。

控制面元数据（``meta.algorithm_registry`` / ``algorithm_events`` / ``data_generation``）
与任务接口同口径：使用控制面写连接读取（只读角色不含 ``meta``）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.schemas import (
    AlgorithmEventOut,
    AlgorithmOut,
    DataGenerationOut,
)

router = APIRouter(tags=["algorithms"])

Context = Annotated[ApiContext, Depends(get_context)]


@router.get("/algorithms", response_model=list[AlgorithmOut], summary="算法登记（注册表）")
def list_algorithms(context: Context) -> list[AlgorithmOut]:
    return [AlgorithmOut.from_row(row) for row in context.algorithms.list_all()]


@router.get(
    "/algorithms/events",
    response_model=list[AlgorithmEventOut],
    summary="算法升级台账",
)
def list_algorithm_events(context: Context) -> list[AlgorithmEventOut]:
    return [AlgorithmEventOut.from_record(event) for event in context.algorithms.list_events()]


@router.get(
    "/algorithms/generations",
    response_model=list[DataGenerationOut],
    summary="投影代次（data_generation）",
)
def list_generations(context: Context) -> list[DataGenerationOut]:
    return [DataGenerationOut.from_row(row) for row in context.algorithms.list_generations()]
