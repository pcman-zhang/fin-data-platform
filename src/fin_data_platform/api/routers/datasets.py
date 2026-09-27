"""数据集（数据字典）浏览：只读，直接来自字典（不依赖数据库）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.json_schema import dataset_json_schema
from fin_data_platform.api.schemas import DatasetDetail, DatasetSummary
from fin_data_platform.query import NotFound

Context = Annotated[ApiContext, Depends(get_context)]

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetSummary], summary="数据集清单")
def list_datasets(
    context: Context,
    domain: Annotated[str | None, Query(description="按数据域过滤")] = None,
    q: Annotated[str | None, Query(description="按数据集名/描述模糊匹配")] = None,
) -> list[DatasetSummary]:
    specs = list(context.specs.values())
    if domain:
        specs = [spec for spec in specs if spec.domain == domain]
    if q:
        needle = q.strip().lower()
        specs = [
            spec
            for spec in specs
            if needle in spec.dataset.lower() or needle in spec.description.lower()
        ]
    return [
        DatasetSummary.from_spec(spec)
        for spec in sorted(specs, key=lambda item: item.dataset)
    ]


@router.get("/{dataset}", response_model=DatasetDetail, summary="数据集详情")
def get_dataset(dataset: str, context: Context) -> DatasetDetail:
    spec = context.specs.get(dataset)
    if spec is None:
        raise NotFound(f"数据集不存在: {dataset}", hint="数据集清单见 GET /v1/datasets")
    return DatasetDetail.from_spec(spec)


@router.get("/{dataset}/schema", summary="字段 JSON Schema（机器可读；与字典同源）")
def get_dataset_schema(dataset: str, context: Context) -> dict[str, Any]:
    spec = context.specs.get(dataset)
    if spec is None:
        raise NotFound(f"数据集不存在: {dataset}", hint="数据集清单见 GET /v1/datasets")
    return dataset_json_schema(spec)
