"""数据字典 → JSON Schema（REST schema 端点；机器可读、与字典同源）。"""

from __future__ import annotations

from typing import Any

from fin_data_platform.dictionary.models import DatasetSpec, FieldSpec

_TYPE_MAP: dict[str, dict[str, Any]] = {
    "int64": {"type": "integer"},
    "float64": {"type": "number"},
    "decimal": {"type": "number"},
    "bool": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "timestamp": {"type": "string", "format": "date-time"},
    "timestamp_tz": {"type": "string", "format": "date-time"},
    "string": {"type": "string"},
    "enum": {"type": "string"},
}


def field_json_schema(field: FieldSpec) -> dict[str, Any]:
    """单字段 JSON Schema 片段（单位 / 枚举 / PIT 角色 / 精度随附）。"""
    schema: dict[str, Any] = dict(_TYPE_MAP.get(field.type, {"type": "string"}))
    schema["description"] = field.description
    if field.unit:
        schema["x-unit"] = field.unit
    if field.enum:
        schema["enum"] = list(field.enum)
    if field.pit_role:
        schema["x-pit-role"] = field.pit_role
    if field.precision is not None:
        schema["x-precision"] = field.precision
    if field.scale is not None:
        schema["x-scale"] = field.scale
    return schema


def dataset_json_schema(spec: DatasetSpec) -> dict[str, Any]:
    """数据集字段 JSON Schema（含业务键 / 物理键 / 语义版本等扩展字段）。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": spec.dataset,
        "description": spec.description,
        "type": "object",
        "properties": {field.name: field_json_schema(field) for field in spec.fields},
        "required": [field.name for field in spec.fields if not field.nullable],
        "x-semantic-version": spec.semantic_version,
        "x-pit-class": spec.pit_class,
        "x-business-key": list(spec.business_key),
        "x-physical-key": list(spec.physical_key),
        "x-grain": spec.grain,
    }
