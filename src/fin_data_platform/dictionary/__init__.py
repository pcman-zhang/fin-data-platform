"""数据字典加载与 CI 校验（doc-11 冻结稿）。

- 目录：本包内 ``<domain>/<dataset>.yaml``（一数据集一文件；路径 = dataset 名）；
- 校验：``validate_directory()`` 实现 doc-11 §6 的 10 条 CI 规则（返回错误列表，空 = 通过）；
- meta-schema：``export_schema()/write_schema()``（CI 与文件比对，防漂移）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fin_data_platform.dictionary.models import DatasetSpec

#: 默认字典根目录（包内资源；部署可显式传入覆盖路径）
DEFAULT_ROOT = Path(__file__).resolve().parent

_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_DATASET_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_ALGORITHM_ID = re.compile(r"^[a-z][a-z0-9_]*_v[0-9]+$")
_IMPLEMENTATION = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")
_BRAND_PREFIX = re.compile(r"^(wind|tushare|ts|akshare|ak|baostock|fuyao|ifind)_")
_EXPR_CHARS = re.compile(r"^[a-z0-9_ ()<>=!&|.,+\-*/]+$")
_EXPR_IDENT = re.compile(r"[a-z_][a-z0-9_]*")
#: 输入口径后缀（derived.inputs 的 @mode；raw 恒可用）
_ADJUST_MODES = frozenset({"raw", "qfq", "hfq"})
_EXPR_KEYWORDS = frozenset({"and", "or", "not", "true", "false"})
_PIT_REQUIRED_KEYS = {
    "market": ("knowledge_time", "version"),
    "versioned": ("knowledge_time", "version"),
    "snapshot": ("knowledge_time", "version"),
}
_SCD2_TIME_KEYS = ("valid_from", "start_date", "in_date")
#: doc-13 §3.1：pit_class → 默认分区策略（覆盖机制后续再引入）
_DEFAULT_PARTITION = {
    "market": "event_time",
    "versioned": "knowledge_time",
    "snapshot": "event_time",
    "scd2": "none",
}
_READ_MODEL = re.compile(r"^mart\.[a-z][a-z0-9_]*_v([0-9]+)$")


def load_file(path: Path) -> DatasetSpec:
    """加载单个字典文件（Pydantic 结构校验）。"""
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"字典文件应为单条目 YAML 对象: {path}")
    return DatasetSpec.model_validate(data)


def load_all(root: Path | None = None) -> dict[str, DatasetSpec]:
    """加载目录下全部字典（跳过 ``_schema`` 等下划线目录）。"""
    base = root or DEFAULT_ROOT
    specs: dict[str, DatasetSpec] = {}
    for path in sorted(base.rglob("*.yaml")):
        relative = path.relative_to(base)
        if any(part.startswith("_") for part in relative.parts):
            continue
        spec = load_file(path)
        if spec.dataset in specs:
            raise ValueError(f"dataset 重复定义: {spec.dataset}")
        specs[spec.dataset] = spec
    return specs


def dataset_path(root: Path, dataset: str) -> Path:
    return root.joinpath(*dataset.split(".")).with_suffix(".yaml")


def validate_directory(root: Path | None = None) -> list[str]:
    """doc-11 §6 CI 校验：返回错误列表（空列表 = 通过）。

    逐文件捕获结构错误（不中断），语义校验对成功加载的条目执行。
    """
    base = root or DEFAULT_ROOT
    errors: list[str] = []
    specs: dict[str, DatasetSpec] = {}
    for path in sorted(base.rglob("*.yaml")):
        relative = path.relative_to(base)
        if any(part.startswith("_") for part in relative.parts):
            continue
        try:
            spec = load_file(path)
        except (ValidationError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative}: 加载失败: {exc}")
            continue
        # 9. 文件路径 ↔ dataset 一致（一数据集一文件）
        path_id = ".".join(relative.with_suffix("").parts)
        if path_id != spec.dataset:
            errors.append(f"{spec.dataset}: 文件路径 {relative} 与 dataset 不一致")
        if spec.dataset in specs:
            errors.append(f"dataset 重复定义: {spec.dataset}")
        specs[spec.dataset] = spec

    seen_algorithms: dict[str, str] = {}
    for dataset, spec in specs.items():
        errors.extend(_validate_dataset(dataset, spec, specs, seen_algorithms))
    errors.extend(_validate_acyclic(specs))
    return errors


def export_schema() -> dict[str, Any]:
    """导出 meta-schema（JSON Schema，doc-11 §2）。"""
    return DatasetSpec.model_json_schema()


#: dataset → 获取方式（对外接口；序列/宏观接入时补充）
_ACCESS: dict[str, str] = {
    "cn_equity.daily_bar": "get_bars(adjust=None/qfq/hfq)",
    "cn_equity.adj_factor": "get_adjust_factors",
    "cn_equity.index_weight": "get_index_weights",
    "cn_equity.index_member": 'get_reference("industry_member")',
    "cn_equity.financials.balance_sheet": 'get_financials(kind="balance_sheet")',
    "cn_equity.market_events.namechange": 'get_market_events(kind="namechange")',
    "cn_equity.listing_lifecycle": 'get_reference("stock_list") / 内部',
    "cn_fund.nav": "get_fund_nav",
    "ref.entity": "get_security_info / 内部",
    "ref.entity_code_history": "内部（Hub mapper）",
    "ref.entity_relation": "内部（关系注册）",
    "ref.entity_external_id": "内部（外部标识注册）",
    "ref.relation_type_dict": "内部（关系词表）",
}

#: 实体键字段：直接挂实体的数据集（issuer_id 指发行主体，亦为 ref.entity）
_ENTITY_FIELDS = frozenset({"entity_id", "issuer_id"})


def catalog(root: Path | None = None) -> list[dict[str, str]]:
    """数据目录：我们有哪些数据、以什么 code/方式获取（doc-18 §5）。"""
    specs = load_all(root or DEFAULT_ROOT)
    rows: list[dict[str, str]] = []
    for dataset, spec in sorted(specs.items()):
        has_entity = any(
            field.name in _ENTITY_FIELDS or field.name.endswith("_entity_id")
            for field in spec.fields
        )
        rows.append(
            {
                "dataset": dataset,
                "domain": str(spec.domain),
                "description": spec.description,
                "entity": "entity" if has_entity else "none",
                "pit_class": spec.pit_class,
                "sources": ", ".join(sorted({str(source.provider) for source in spec.sources}))
                or "—",
                "access": _ACCESS.get(dataset, "—"),
            }
        )
    return rows


def catalog_markdown(root: Path | None = None) -> str:
    """数据目录（Markdown，自动生成）。"""
    lines = [
        "# 数据目录（自动生成）",
        "",
        "> 回答两个基础问题：**我们有哪些数据**、**以什么 code / 方式获取**（doc-18 §5）；",
        "> 随数据字典更新重新生成，请勿手改。",
        "",
        "| dataset | 域 | 说明 | 实体 | PIT 类别 | 来源 | 获取方式 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in catalog(root):
        lines.append(
            f"| `{row['dataset']}` | {row['domain']} | {row['description']} | "
            f"{row['entity']} | {row['pit_class']} | {row['sources']} | "
            f"`{row['access']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def schema_path(root: Path | None = None) -> Path:
    base = root or DEFAULT_ROOT
    return base / "_schema" / "dictionary.schema.json"


def write_schema(root: Path | None = None) -> Path:
    """生成/刷新 meta-schema 文件（CI 比对用）。"""
    target = schema_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_schema_text(), encoding="utf-8")
    return target


def _schema_text() -> str:
    return json.dumps(export_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _validate_dataset(
    dataset: str,
    spec: DatasetSpec,
    specs: dict[str, DatasetSpec],
    seen_algorithms: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    prefix = f"{dataset}"

    if not _DATASET_ID.match(dataset):
        errors.append(f"{prefix}: dataset 命名非法")
    if not dataset.startswith(f"{spec.domain}."):
        errors.append(f"{prefix}: domain={spec.domain} 与 dataset 前缀不一致")

    field_names = [field.name for field in spec.fields]
    field_set = set(field_names)
    if len(field_set) != len(field_names):
        errors.append(f"{prefix}: 字段名重复")

    # 3. 命名规范：小写蛇形 + 禁供应商品牌前缀（Source Independence）
    for field in spec.fields:
        if not _FIELD_NAME.match(field.name):
            errors.append(f"{prefix}.{field.name}: 字段命名非法（小写蛇形）")
        if _BRAND_PREFIX.match(field.name):
            errors.append(f"{prefix}.{field.name}: 字段含供应商品牌前缀（doc-10 §6.2）")

    # 5. business/physical key
    if not spec.business_key:
        errors.append(f"{prefix}: business_key 为空")
    missing_business = [k for k in spec.business_key if k not in field_set]
    if missing_business:
        errors.append(f"{prefix}: business_key 引用不存在的字段 {missing_business}")
    if not set(spec.business_key) <= set(spec.physical_key):
        errors.append(f"{prefix}: physical_key 必须包含 business_key")
    missing_physical = [k for k in spec.physical_key if k not in field_set]
    if missing_physical:
        errors.append(f"{prefix}: physical_key 引用不存在的字段 {missing_physical}")
    required_time_keys = _PIT_REQUIRED_KEYS.get(spec.pit_class)
    if required_time_keys:
        absent = [k for k in required_time_keys if k not in spec.physical_key]
        if absent:
            errors.append(f"{prefix}: {spec.pit_class} 的 physical_key 缺少 {absent}")
    elif spec.pit_class == "scd2" and not any(k in spec.physical_key for k in _SCD2_TIME_KEYS):
        errors.append(f"{prefix}: scd2 的 physical_key 缺少区间列 {list(_SCD2_TIME_KEYS)}")

    # 6. lineage / derived 依赖
    for upstream in spec.lineage.upstream:
        if upstream.dataset not in specs:
            errors.append(f"{prefix}: lineage 上游不存在 {upstream.dataset}")
    for entry in spec.derived or []:
        if not _ALGORITHM_ID.match(entry.algorithm_id):
            errors.append(f"{prefix}: algorithm_id 命名非法 {entry.algorithm_id!r}")
        if not _IMPLEMENTATION.match(entry.implementation):
            errors.append(f"{prefix}: implementation 非点分路径 {entry.implementation!r}")
        if entry.algorithm_id in seen_algorithms:
            errors.append(
                f"{prefix}: algorithm_id 重复 {entry.algorithm_id}"
                f"（已被 {seen_algorithms[entry.algorithm_id]} 使用）"
            )
        seen_algorithms[entry.algorithm_id] = dataset
        if not _FIELD_NAME.match(entry.output):
            errors.append(f"{prefix}: derived.output 命名非法 {entry.output!r}")
        if entry.output in field_set:
            errors.append(f"{prefix}: derived.output 与物理字段同名 {entry.output!r}")
        for ref in entry.inputs:
            base, _, mode = ref.partition("@")
            ref_dataset, _, ref_field = base.rpartition(".")
            if ref_dataset not in specs:
                errors.append(f"{prefix}: derived 输入数据集不存在 {ref_dataset}")
                continue
            ref_spec = specs[ref_dataset]
            known_fields = {item.name for item in ref_spec.fields}
            known_outputs = {item.output for item in (ref_spec.derived or [])}
            if ref_field in known_outputs:
                if mode:
                    errors.append(
                        f"{prefix}: 因子输出不支持口径后缀：{ref}"
                        "（因子值为计算结果，口径在登记输入时确定）"
                    )
            elif ref_field not in known_fields:
                errors.append(f"{prefix}: derived 输入字段不存在 {base}")
            if mode:
                if mode not in _ADJUST_MODES:
                    errors.append(
                        f"{prefix}: derived 输入口径非法 {mode!r}（可选: {sorted(_ADJUST_MODES)}）"
                    )
                elif mode != "raw":
                    declared = ref_spec.adjust if ref_spec.adjust else None
                    if declared is None or mode not in declared.modes:
                        errors.append(f"{prefix}: 数据集未声明口径 {mode}: {ref_dataset}")
                    elif ref_field not in declared.fields:
                        errors.append(
                            f"{prefix}: 派生输入字段不可复权：{base}"
                            f"（可复权字段 {declared.fields}）"
                        )

    # 6.5 adjust 声明（复权口径；访问面执行）
    if spec.adjust is not None:
        adjust = spec.adjust
        if adjust.factor_dataset == dataset:
            errors.append(f"{prefix}: adjust.factor_dataset 不得为自身")
        elif adjust.factor_dataset not in specs:
            errors.append(f"{prefix}: adjust.factor_dataset 不存在 {adjust.factor_dataset}")
        else:
            factor_spec = specs[adjust.factor_dataset]
            factor_fields = {f.name for f in factor_spec.fields}
            if adjust.factor_field not in factor_fields:
                errors.append(
                    f"{prefix}: adjust.factor_field 不存在于 {adjust.factor_dataset}"
                    f"：{adjust.factor_field}"
                )
            if not set(factor_spec.business_key) <= set(spec.business_key):
                errors.append(
                    f"{prefix}: 因子数据集业务键须为 {dataset} 业务键的子集"
                    f"（{factor_spec.business_key} ⊄ {spec.business_key}）"
                )
            factor_event = next(
                (item.name for item in factor_spec.fields if item.pit_role == "event_time"),
                None,
            )
            if not [name for name in factor_spec.business_key if name != factor_event]:
                errors.append(
                    f"{prefix}: adjust.factor_dataset 缺少非事件时间业务键（无法计算 as-of 锚点）"
                )
        missing = [name for name in adjust.fields if name not in field_set]
        if missing:
            errors.append(f"{prefix}: adjust.fields 不存在 {missing}")

    # 7. quality 规则
    for index, rule in enumerate(spec.quality):
        label = f"{prefix}.quality[{index}]"
        for key in rule.keys or []:
            if key not in field_set:
                errors.append(f"{label}: keys 引用不存在字段 {key}")
        for name in (rule.fields or []) + ([rule.field] if rule.field else []):
            if name not in field_set:
                errors.append(f"{label}: 引用不存在字段 {name}")
        if rule.expr is not None:
            errors.extend(_validate_expression(label, rule.expr, field_set))

    # 8. mappings（canonical 字段必须存在；仅字段名，不含换算；endpoint 必须在 sources 声明）
    source_endpoints = {(entry.provider, entry.endpoint) for entry in spec.sources}
    for mapping in spec.mappings:
        if (mapping.provider, mapping.endpoint) not in source_endpoints:
            errors.append(
                f"{prefix}.mappings: provider={mapping.provider} "
                f"endpoint={mapping.endpoint} 未在 sources 中声明"
            )
        for canonical in mapping.fields:
            if canonical not in field_set:
                errors.append(f"{prefix}.mappings[{mapping.provider}]: 未知字段 {canonical}")

    # storage：canonical_table / read_model / partition_strategy 一致性（doc-13 §9）
    expected_table = f"{spec.domain}." + "_".join(dataset.split(".")[1:])
    if spec.storage.canonical_table != expected_table:
        errors.append(
            f"{prefix}: canonical_table 应为 {expected_table}"
            f"（实际 {spec.storage.canonical_table}）"
        )
    read_model = _READ_MODEL.match(spec.storage.read_model)
    if read_model is None:
        errors.append(
            f"{prefix}: read_model 应为 mart.<name>_v<version>（实际 {spec.storage.read_model}）"
        )
    elif int(read_model.group(1)) != spec.semantic_version:
        errors.append(
            f"{prefix}: read_model 版本后缀与 semantic_version={spec.semantic_version} 不一致"
        )
    expected_partition = _DEFAULT_PARTITION[spec.pit_class]
    if spec.storage.partition_strategy != expected_partition:
        errors.append(
            f"{prefix}: partition_strategy 应为 {expected_partition}（pit_class={spec.pit_class}）"
        )
    if spec.storage.partition_strategy != "none" and spec.storage.compression is None:
        errors.append(
            f"{prefix}: partition_strategy={spec.storage.partition_strategy} 需要 compression"
        )

    return errors


def _validate_expression(label: str, expr: str, field_set: set[str]) -> list[str]:
    errors: list[str] = []
    if not _EXPR_CHARS.match(expr):
        return [f"{label}: expression 含不允许字符（仅比较/逻辑/算术）"]
    for ident in _EXPR_IDENT.findall(expr):
        if ident in _EXPR_KEYWORDS:
            continue
        if ident not in field_set:
            errors.append(f"{label}: expression 引用不存在字段 {ident}")
    return errors


def _validate_acyclic(specs: dict[str, DatasetSpec]) -> list[str]:
    edges: dict[str, set[str]] = {dataset: set() for dataset in specs}
    for dataset, spec in specs.items():
        for upstream in spec.lineage.upstream:
            if upstream.dataset in edges:
                edges[upstream.dataset].add(dataset)
        for entry in spec.derived or []:
            for ref in entry.inputs:
                ref_dataset = ref.rpartition(".")[0]
                # 同数据集内的派生（如 qfq_close 用本表 close）不构成依赖环
                if ref_dataset in edges and ref_dataset != dataset:
                    edges[ref_dataset].add(dataset)

    state: dict[str, int] = {}
    errors: list[str] = []

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in sorted(edges[node]):
            if state.get(nxt) == 1:
                errors.append(f"血缘成环: {' -> '.join([*path, node, nxt])}")
            elif state.get(nxt) is None:
                visit(nxt, [*path, node])
        state[node] = 2

    for node in sorted(edges):
        if state.get(node) is None:
            visit(node, [])
    return errors
