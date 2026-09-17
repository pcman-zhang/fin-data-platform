"""数据字典 meta-schema（doc-11，冻结稿）。

Pydantic v2 严格模型（``extra="forbid"``）：结构校验在模型层，语义/跨字段校验在
:mod:`fin_data_platform.dictionary` 的 ``validate_*``（CI 门禁）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


#: 数据域（doc-10 §3.1）
class Domain(StrEnum):
    CN_EQUITY = "cn_equity"
    CN_FUND = "cn_fund"
    CN_FUTURES = "cn_futures"
    CN_OPTIONS = "cn_options"
    HK_EQUITY = "hk_equity"
    US_EQUITY = "us_equity"
    US_OPTIONS = "us_options"
    MACRO_CN = "macro_cn"
    MACRO_US = "macro_us"
    MACRO_GLOBAL = "macro_global"
    REF = "ref"


class Provider(StrEnum):
    TUSHARE = "tushare"
    BAOSTOCK = "baostock"
    WIND = "wind"
    AKSHARE = "akshare"
    FUYAO = "fuyao"
    IFIND = "ifind"


LogicalType = Literal[
    "int64",
    "float64",
    "decimal",
    "string",
    "bool",
    "date",
    "timestamp",
    "timestamp_tz",
    "enum",
]
PitRole = Literal[
    "event_time",
    "publish_time",
    "knowledge_time",
    "ingest_time",
    "none",
]
PitClass = Literal["market", "versioned", "scd2", "snapshot"]


class Materialize(StrEnum):
    """派生物化策略（doc-11 §4）：none=0 存储；latest=单份可重建投影。"""

    NONE = "none"
    LATEST = "latest"


class Refresh(StrEnum):
    """派生刷新策略（doc-11 §4）：on_demand=按需；scheduled=随调度任务刷新。"""

    ON_DEMAND = "on_demand"
    SCHEDULED = "scheduled"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RangeSpec(_Base):
    min: float | int | str | None = None
    max: float | int | str | None = None


class FieldSpec(_Base):
    name: str
    type: LogicalType
    precision: int | None = None
    scale: int | None = None
    unit: str | None = None
    nullable: bool
    enum: list[str] | None = None
    range: RangeSpec | None = None
    description: str
    pit_role: PitRole

    @model_validator(mode="after")
    def _validate_type_details(self) -> FieldSpec:
        if self.type == "decimal":
            if self.precision is None or self.scale is None:
                raise ValueError(f"decimal 字段 {self.name} 必须提供 precision/scale")
        elif self.precision is not None or self.scale is not None:
            raise ValueError(f"非 decimal 字段 {self.name} 不应提供 precision/scale")
        if self.type == "enum" and not self.enum:
            raise ValueError(f"enum 字段 {self.name} 必须提供枚举值")
        return self


class UpdateSla(_Base):
    frequency: str
    earliest_available: str
    latest_available: str
    tolerance: str


class SourceEntry(_Base):
    provider: Provider
    endpoint: str
    note: str | None = None


class ExpectedDates(_Base):
    calendar: str
    frequency: str


class Coverage(_Base):
    universe: str
    universe_source: str
    history_start: str
    expected_dates: ExpectedDates


class Compression(_Base):
    after: str
    segment_by: str
    order_by: str


class Storage(_Base):
    canonical_table: str
    read_model: str
    read_model_impl: Literal["view", "projection_table"]
    partition_strategy: Literal["event_time", "knowledge_time", "none"]
    partition_interval: str
    retention: str
    #: 仅 hypertable（partition_strategy != none）需要；由 validate 强制
    compression: Compression | None = None


class AdjustSpec(_Base):
    """复权口径声明（访问面执行；doc-11 §3.7）。

    - ``modes``：支持的口径（``raw`` 恒可用，无需列出）；
    - ``factor_dataset`` / ``factor_field``：因子来源（如 ``cn_equity.adj_factor``）；
    - ``fields``：可复权字段（显式声明，读取层不推断）；
    - ``default``：读取缺省口径（``none`` = 不调整）。
    """

    modes: list[Literal["qfq", "hfq"]]
    factor_dataset: str
    factor_field: str
    fields: list[str]
    default: Literal["none", "qfq", "hfq"] = "none"

    @model_validator(mode="after")
    def _validate_adjust(self) -> AdjustSpec:
        if not self.modes:
            raise ValueError("adjust.modes 不能为空（raw 恒可用，无需列出）")
        if len(set(self.modes)) != len(self.modes):
            raise ValueError("adjust.modes 不得重复")
        if self.default != "none" and self.default not in self.modes:
            raise ValueError(f"adjust.default={self.default} 不在 modes 内且非 none")
        if not self.fields:
            raise ValueError("adjust.fields 不能为空（明确哪些字段可复权）")
        return self


class QualityRule(_Base):
    rule: Literal[
        "unique", "not_null", "range", "enum", "expression", "reconcile", "freshness"
    ]
    keys: list[str] | None = None
    field: str | None = None
    fields: list[str] | None = None
    min: float | None = None
    max: float | None = None
    values: list[str] | None = None
    expr: str | None = None
    severity: Literal["error", "warn"] | None = None
    against: str | None = None
    sla: str | None = None
    tolerance: str | None = None

    @model_validator(mode="after")
    def _validate_rule_args(self) -> QualityRule:
        required: tuple[str, ...] = {
            "unique": ("keys",),
            "not_null": ("fields",),
            "range": ("field",),
            "enum": ("field", "values"),
            "expression": ("expr",),
            "reconcile": ("against",),
            "freshness": ("sla", "tolerance"),
        }[self.rule]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"quality 规则 {self.rule} 缺少参数 {missing}")
        return self


class UpstreamRef(_Base):
    dataset: str
    fields: list[str] | None = None


class Lineage(_Base):
    upstream: list[UpstreamRef]
    transform: str


class DerivedEntry(_Base):
    output: str
    algorithm_id: str
    implementation: str
    owner: str
    inputs: list[str]
    description: str
    #: 物化策略（doc-11 §4）：none=0 存储（读模型内联/按需计算）| latest=单份可重建投影
    materialize: Materialize = Materialize.NONE
    #: 刷新策略（doc-11 §4）：on_demand=引擎/API 触发 | scheduled=随调度任务刷新
    refresh: Refresh = Refresh.ON_DEMAND


class MappingEntry(_Base):
    provider: Provider
    endpoint: str
    fields: dict[str, str]


class DatasetSpec(_Base):
    dataset: str
    semantic_version: int
    domain: Domain
    description: str
    pit_class: PitClass
    business_key: list[str]
    physical_key: list[str]
    grain: str
    update_sla: UpdateSla
    sources: list[SourceEntry]
    coverage: Coverage
    storage: Storage
    quality: list[QualityRule]
    lineage: Lineage
    #: 复权口径声明（可复权数据集；不登记 = 无复权）
    adjust: AdjustSpec | None = None
    derived: list[DerivedEntry] | None = None
    mappings: list[MappingEntry]
    fields: list[FieldSpec]
