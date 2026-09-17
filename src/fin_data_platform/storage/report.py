"""数据库文档生成（Schema First）：表/字段/依赖 → Markdown。

由字典 + 实体注册表 schema 自动生成，保证文档与 DDL 同源（doc-11 §7）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Column

from fin_data_platform.dictionary import DEFAULT_ROOT
from fin_data_platform.dictionary.models import DatasetSpec
from fin_data_platform.storage.schema import build_metadata

_REF_TABLES = {
    "ref.entity": "实体注册表（实体身份 + 分类面 + PIT 属性；SCD2）",
    "ref.entity_code_history": "canonical 代码履历（代码变更/复用）",
    "ref.entity_relation": "实体关系（单向存储；inverse 词表驱动双向查询）",
    "ref.entity_external_id": "实体外部标识（isin/figi/cusip/sedol/lei/uscc）",
    "ref.relation_type_dict": "关系词表（关系类型登记）",
    "meta.job_defs": "Runtime 任务定义镜像（声明式注册；doc-20）",
    "meta.job_dependencies": "任务依赖与触发条件（parent_job / child_job / condition）",
    "meta.job_runs": "任务运行记录与状态机（Runtime 状态权威）",
    "meta.watermarks": "数据集 / 分区水位",
    "meta.algorithm_registry": "派生算法登记（历史 id 永久保留）",
    "meta.algorithm_events": "算法升级 / 重述台账（algorithm_id / effective_from / reason）",
    "meta.data_generation": "读模型 / 派生投影构建代次（doc-12 X-Data-Generation）",
}

#: 逻辑引用 ref.entity 的字段名（issuer_id 指发行主体）
_ENTITY_FIELDS = frozenset({"entity_id", "issuer_id"})


def _purpose(dataset: str, spec: DatasetSpec | None) -> str:
    if spec is not None:
        return spec.description
    return _REF_TABLES.get(dataset, "")


def _column_type(column: Column) -> str:
    return str(column.type)


def database_markdown(root: Path | None = None) -> str:
    """生成数据库设计文档（表作用 / 字段与类型 / 依赖关系）。"""
    metadata, specs = build_metadata(root or DEFAULT_ROOT)
    lines: list[str] = [
        "# 数据库设计：表 / 字段 / 依赖（自动生成）",
        "",
        "> 由数据字典与 实体注册表 schema 生成（Schema First）；请勿手改，变更走字典。",
        "",
        "## 1. 表清单与作用",
        "",
        "| 表 | 作用 | 物理键 | 分区策略 |",
        "|---|---|---|---|",
    ]
    by_dataset = {spec.storage.canonical_table: (dataset, spec) for dataset, spec in specs.items()}
    table_order = sorted(metadata.tables, key=lambda key: (key.split(".")[0], key))
    for key in table_order:
        dataset, spec = by_dataset.get(key, (key, None))
        pk = ", ".join(column.name for column in metadata.tables[key].primary_key)
        strategy = spec.storage.partition_strategy if spec else "—"
        lines.append(f"| `{key}` | {_purpose(dataset, spec)} | {pk} | {strategy} |")

    lines += ["", "## 2. 字段与类型", ""]
    for key in table_order:
        dataset, spec = by_dataset.get(key, (key, None))
        table = metadata.tables[key]
        lines.append(f"### `{key}`")
        lines.append("")
        lines.append("| 字段 | 类型 | 可空 | 单位 | PIT 角色 | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        field_by_name = {field.name: field for field in (spec.fields if spec else [])}
        for column in table.columns:
            field = field_by_name.get(column.name)
            unit = (field.unit if field else None) or ""
            pit = field.pit_role if field else ""
            description = field.description if field else ""
            nullable = "是" if column.nullable else "否"
            lines.append(
                f"| `{column.name}` | `{_column_type(column)}` | {nullable} | "
                f"{unit} | {pit} | {description} |"
            )
        lines.append("")

    lines += ["## 3. 表依赖关系（逻辑，无物理外键；doc-13 §4）", ""]
    lines += [
        "> `entity_id`/`issuer_id` 为平台稳定代理键（BIGINT，代理键非源代码）；",
        "> 按 doc-13 §4 **默认不建物理外键**（hypertable 压缩与批量回填约束、",
        "> SCD2 主键为 `(entity_id, valid_from)` 无法被事实表单列引用）。",
        "> 引用完整性由三层保障：① 写入管线校验；",
        "> ② 质量规则（`reconcile against: ref.entity`）；",
        "> ③ 读取时以 `entity_id` 关联 `ref.entity`（名称/类型/退市属性）。",
        "",
    ]
    for key in table_order:
        dataset, spec = by_dataset.get(key, (key, None))
        dependencies: list[str] = []
        if spec is not None:
            dependencies.extend(
                f"{ref.dataset}（血缘）" for ref in spec.lineage.upstream
            )
            for entry in spec.derived or []:
                for ref in entry.inputs:
                    ref_dataset = ref.rpartition(".")[0]
                    if ref_dataset == dataset:
                        continue  # 同数据集内派生不构成表依赖
                    dependencies.append(f"{ref_dataset}（派生输入）")
        table = metadata.tables[key]
        if any(
            column.name in _ENTITY_FIELDS or column.name.endswith("_entity_id")
            for column in table.columns
        ) and key != "ref.entity":
            dependencies.append("ref.entity（entity_id/issuer_id 逻辑引用）")
        if key in {"ref.entity_code_history", "ref.entity_relation", "ref.entity_external_id"}:
            dependencies.append("ref.entity（血缘）")
        unique = sorted(set(dependencies))
        lines.append(f"- `{key}` ← {'；'.join(unique) if unique else '—（源数据）'}")
    lines.append("")
    return "\n".join(lines)
