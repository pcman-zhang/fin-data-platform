"""实体注册表浏览：检索 / 详情（时间轴、代码履历、关系、外部标识）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from fin_data_platform.api.deps import ApiContext, get_context
from fin_data_platform.api.schemas import (
    CodeOut,
    EntityDetailOut,
    EntityListOut,
    EntitySummary,
    ExternalIdOut,
    RelationOut,
    RelationTypeOut,
)
from fin_data_platform.registry.universe import universe

router = APIRouter(prefix="/entities", tags=["entities"])

Context = Annotated[ApiContext, Depends(get_context)]


@router.get("", response_model=EntityListOut, summary="实体检索")
def list_entities(
    context: Context,
    query: Annotated[str | None, Query(description="代码/名称模糊匹配")] = None,
    entity_type: Annotated[str | None, Query()] = None,
    market: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntityListOut:
    records, total = context.registry.search_entities(
        query=query,
        entity_type=entity_type,
        market=market,
        limit=limit,
        offset=offset,
    )
    return EntityListOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[EntitySummary.from_record(record) for record in records],
    )


@router.get(
    "/relation-types", response_model=list[RelationTypeOut], summary="关系词表"
)
def list_relation_types(context: Context) -> list[RelationTypeOut]:
    return [
        RelationTypeOut(
            relation_type=item.relation_type,
            inverse_relation=item.inverse_relation,
            description=item.description,
        )
        for item in context.registry.relation_types()
    ]


@router.get(
    "/universe",
    response_model=EntityListOut,
    summary="PIT Universe（某事件日在市标的）",
)
def entity_universe(
    context: Context,
    as_of: Annotated[date, Query(description="事件日期：该日在市（listed/suspended）")],
    knowledge_as_of: Annotated[
        datetime | None,
        Query(description="知识时点（可选）：仅纳入当时已知的状态行；缺省=当前知识"),
    ] = None,
    entity_type: Annotated[str | None, Query()] = None,
    market: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntityListOut:
    """由交易状态数据集（listing_lifecycle）推导的 as-of PIT Universe（分页）。"""
    records = universe(
        context.registry,
        context.registry.lifecycle(knowledge_as_of=knowledge_as_of),
        as_of,
        knowledge_as_of=knowledge_as_of,
        entity_type=entity_type,
        market=market,
    )
    window = records[offset : offset + limit]
    return EntityListOut(
        total=len(records),
        limit=limit,
        offset=offset,
        items=[EntitySummary.from_record(record) for record in window],
    )


@router.get("/{entity_id}", response_model=EntityDetailOut, summary="实体详情")
def get_entity(entity_id: int, context: Context) -> EntityDetailOut:
    record = context.registry.entity(entity_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"实体不存在: {entity_id}")
    history = context.registry.entity_history(entity_id)
    codes = context.registry.code_history(entity_id)
    relations = context.registry.relations(entity_id)
    external_ids = context.registry.external_ids(entity_id)
    return EntityDetailOut(
        **EntitySummary.from_record(record).model_dump(),
        history=[EntitySummary.from_record(item) for item in history],
        code_history=[
            CodeOut(
                code=item.code,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
                version=item.version,
            )
            for item in codes
        ],
        relations=[
            RelationOut(
                relation_type=item.relation_type,
                direction=item.direction,
                related_id=item.related_id,
                related_code=item.related_code,
                related_name=item.related_name,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
            for item in relations
        ],
        external_ids=[
            ExternalIdOut(
                id_type=item.id_type,
                id_value=item.id_value,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
            for item in external_ids
        ],
    )
