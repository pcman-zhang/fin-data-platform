"""配额与成本聚合（TASK-3.19，读端）。

按源返回当日调用 / 成本 / 告警与限流配置；口径为**共享缓存计数**
（Runtime 多进程聚合，不依赖单进程 ``hub.stats``）。缓存未配置或不可用时
``shared=false``（fail-open，用量按 0 返回，采集不受影响）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.schemas import UsageOut
from fin_data_platform.quota import read_usage

router = APIRouter(tags=["usage"])

Context = Annotated[ApiContext, Depends(get_context)]


@router.get("/usage", response_model=UsageOut, summary="配额与成本（多进程共享口径）")
def usage(context: Context) -> UsageOut:
    return UsageOut.model_validate(
        read_usage(context.cache, settings=context.quota)
    )
