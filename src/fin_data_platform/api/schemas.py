"""管理 API 响应/请求契约（OpenAPI 来源）。

字段口径来自数据字典（doc-14 §4.3：页面不硬编码口径）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fin_data_platform.derived.store import AlgorithmEvent, AlgorithmRow, DataGenerationRow
from fin_data_platform.dictionary.models import DatasetSpec, FieldSpec
from fin_data_platform.registry.models import EntityRecord
from fin_data_platform.runtime.models import JobRun, Watermark


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- 数据集
class FieldOut(_Base):
    name: str
    type: str
    unit: str | None = None
    nullable: bool
    enum: list[str] | None = None
    precision: int | None = None
    scale: int | None = None
    description: str
    pit_role: str

    @classmethod
    def from_spec(cls, field: FieldSpec) -> FieldOut:
        return cls(
            name=field.name,
            type=field.type,
            unit=field.unit,
            nullable=field.nullable,
            enum=field.enum,
            precision=field.precision,
            scale=field.scale,
            description=field.description,
            pit_role=field.pit_role,
        )


class DatasetSummary(_Base):
    dataset: str
    domain: str
    description: str
    semantic_version: int
    pit_class: str
    business_key: list[str]
    physical_key: list[str]
    grain: str
    canonical_table: str
    read_model: str
    partition_strategy: str
    update_frequency: str
    quality_rules: int
    upstream: list[str]

    @classmethod
    def from_spec(cls, spec: DatasetSpec) -> DatasetSummary:
        return cls(
            dataset=spec.dataset,
            domain=spec.domain,
            description=spec.description,
            semantic_version=spec.semantic_version,
            pit_class=spec.pit_class,
            business_key=spec.business_key,
            physical_key=spec.physical_key,
            grain=spec.grain,
            canonical_table=spec.storage.canonical_table,
            read_model=spec.storage.read_model,
            partition_strategy=spec.storage.partition_strategy,
            update_frequency=spec.update_sla.frequency,
            quality_rules=len(spec.quality),
            upstream=[ref.dataset for ref in spec.lineage.upstream],
        )


class DatasetDetail(DatasetSummary):
    update_sla: dict[str, str]
    fields: list[FieldOut]
    coverage: dict[str, Any]
    storage: dict[str, Any]
    quality: list[dict[str, Any]]
    lineage: dict[str, Any]
    derived: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    mappings: list[dict[str, Any]]

    @classmethod
    def from_spec(cls, spec: DatasetSpec) -> DatasetDetail:
        base = DatasetSummary.from_spec(spec).model_dump()
        return cls(
            **base,
            update_sla=spec.update_sla.model_dump(),
            fields=[FieldOut.from_spec(field) for field in spec.fields],
            coverage=spec.coverage.model_dump(),
            storage=spec.storage.model_dump(),
            quality=[rule.model_dump() for rule in spec.quality],
            lineage=spec.lineage.model_dump(),
            derived=[entry.model_dump() for entry in (spec.derived or [])],
            sources=[entry.model_dump() for entry in spec.sources],
            mappings=[entry.model_dump() for entry in spec.mappings],
        )


# ---------------------------------------------------------------- 实体
class EntitySummary(_Base):
    entity_id: int
    entity_type: str
    entity_class: str | None = None
    market: str | None = None
    code: str
    name: str
    currency: str | None = None
    exchange: str | None = None
    social_status: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    knowledge_time: datetime | None = None
    version: int

    @classmethod
    def from_record(cls, record: EntityRecord) -> EntitySummary:
        return cls(
            entity_id=record.entity_id,
            entity_type=record.entity_type,
            entity_class=record.entity_class,
            market=record.market,
            code=record.code,
            name=record.name,
            currency=record.currency,
            exchange=record.exchange,
            social_status=record.social_status,
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            knowledge_time=record.knowledge_time,
            version=record.version,
        )


class CodeOut(_Base):
    code: str
    valid_from: date | None = None
    valid_to: date | None = None
    version: int


class RelationOut(_Base):
    relation_type: str
    direction: str
    related_id: int
    related_code: str | None = None
    related_name: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class ExternalIdOut(_Base):
    id_type: str
    id_value: str
    valid_from: date | None = None
    valid_to: date | None = None


class RelationTypeOut(_Base):
    relation_type: str
    inverse_relation: str
    description: str


class EntityListOut(_Base):
    total: int
    limit: int
    offset: int
    items: list[EntitySummary]


class EntityDetailOut(EntitySummary):
    history: list[EntitySummary]
    code_history: list[CodeOut]
    relations: list[RelationOut]
    external_ids: list[ExternalIdOut]


# ---------------------------------------------------------------- 任务 / 水位
class JobRunOut(_Base):
    run_id: int
    job_key: str
    job_id: str
    kind: str
    dataset: str
    scope: str
    status: str
    attempt: int
    max_attempts: int
    priority: int
    scheduled_at: datetime
    window_start: date | None = None
    window_end: date | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rows_written: int | None = None
    error: str | None = None
    request_id: str | None = None
    worker: str | None = None

    @classmethod
    def from_run(cls, run: JobRun) -> JobRunOut:
        return cls.model_validate(run)


class WatermarkOut(_Base):
    dataset: str
    scope: str
    watermark_time: datetime | None = None

    @classmethod
    def from_watermark(cls, mark: Watermark) -> WatermarkOut:
        return cls.model_validate(mark)


class SyncRequest(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=200, description="canonical 代码")
    dataset: str = Field(default="cn_equity.daily_bar", description="目标数据集")
    start: date | None = Field(default=None, description="窗口起点（缺省：水位+1）")
    end: date | None = Field(
        default=None, description="窗口终点（缺省：今日；不得晚于今天）"
    )
    request_id: str | None = Field(
        default=None, description="幂等键：重复提交返回既有运行（同时写入运行记录）"
    )
    priority: int = Field(default=100, ge=1, le=999)

    @model_validator(mode="after")
    def _validate_window(self) -> SyncRequest:
        from fin_data_platform.runtime._util import utcnow

        today = utcnow().date()
        if self.end is not None and self.end > today:
            raise ValueError(f"end 不得晚于今天（UTC {today}）：{self.end}")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(f"start 不得晚于 end：{self.start} > {self.end}")
        return self


class SyncItem(BaseModel):
    code: str
    job_id: str
    run_id: int | None = None
    status: str
    window_start: date | None = None
    window_end: date | None = None
    note: str | None = None


class SyncResponse(BaseModel):
    submitted: list[SyncItem]
    skipped: list[SyncItem]


# ---------------------------------------------------------------- 派生算法（doc-10 §3.5）
class AlgorithmOut(_Base):
    algorithm_id: str
    version: int
    owner: str
    implementation: str
    dataset: str | None = None
    output: str | None = None
    inputs: list[str] = Field(default_factory=list)
    description: str
    status: str
    effective_from: date | None = None

    @classmethod
    def from_row(cls, row: AlgorithmRow) -> AlgorithmOut:
        return cls(
            algorithm_id=row.algorithm_id,
            version=row.version,
            owner=row.owner,
            implementation=row.implementation,
            dataset=row.dataset,
            output=row.output,
            inputs=list(row.inputs),
            description=row.description,
            status=row.status,
            effective_from=row.effective_from,
        )


class AlgorithmEventOut(_Base):
    algorithm_id: str
    effective_from: date
    reason: str

    @classmethod
    def from_record(cls, event: AlgorithmEvent) -> AlgorithmEventOut:
        return cls(
            algorithm_id=event.algorithm_id,
            effective_from=event.effective_from,
            reason=event.reason,
        )


class DataGenerationOut(_Base):
    read_model: str
    generation: str
    updated_at: datetime

    @classmethod
    def from_row(cls, row: DataGenerationRow) -> DataGenerationOut:
        return cls(
            read_model=row.read_model,
            generation=row.generation,
            updated_at=row.updated_at,
        )


class HealthOut(BaseModel):
    ok: bool
    checks: dict[str, bool]
    errors: list[str]
