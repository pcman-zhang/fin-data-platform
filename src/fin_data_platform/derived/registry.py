"""派生算法注册表（doc-10 §3.5 / doc-11 §4）。

- 算法 = **代码实现 + 审计标识**：``@register(algorithm_id=...)`` 登记；
  ``algorithm_id`` 稳定（如 ``ma20``），语义版本由 ``version`` 承载，升级 = 升
  ``version`` + 写升级台账（``meta.algorithm_events``），旧版本实现与投影永久保留；
  兼容旧写法 ``xxx_v1`` 后缀（等价于 ``xxx`` + ``version=1``）；
- ``implementation`` 必须等于被装饰函数的真实模块路径（``module.qualname``），
  防止"登记与实现漂移"；
- 结构性错误（命名 / 版本 / 同 (id, version) 冲突）注册时立即拒绝；软性 CI 规则
  （docstring 含 ``Formula`` / ``PIT``）由 :meth:`AlgorithmRegistry.validate` 汇总，
  供 CI 与同步入口统一报告。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypeVar

#: doc-11 §4：稳定审计标识（升级 = 升 ``version``，禁止原地覆盖）
ALGORITHM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
IMPLEMENTATION_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")
_VERSION_PATTERN = re.compile(r"_v([0-9]+)$")

#: doc-11 §4 CI：实现 docstring 必须包含的标记（公式与 PIT 语义说明）
DOCSTRING_MARKERS = ("Formula", "PIT")

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class AlgorithmSpec:
    """代码侧登记信息（``output / inputs / description`` 由字典侧补齐）。"""

    algorithm_id: str
    version: int
    owner: str
    implementation: str
    function: Callable[..., Any] = field(compare=False, repr=False)
    effective_from: date | None = None
    reason: str = ""
    #: 读模型内联 SQL 模板（可选；输入引用以 :func:`inputs.input_view_name` 命名）
    inline_sql: str | None = None

    @property
    def identity(self) -> str:
        """审计身份 ``id@vN``（投影比对 / 指纹 / 任务版本维度用）。"""
        return f"{self.algorithm_id}@v{self.version}"

    @property
    def docstring(self) -> str:
        return inspect.getdoc(self.function) or ""


class AlgorithmRegistry:
    """进程内算法注册表：重复导入幂等，同 ``(id, version)`` 冲突立即报错。

    同一 ``algorithm_id`` 可并存多个 ``version``（升级与历史 pin）；``get(id)``
    缺省返回该 id 的**最高版本**。DB 级多版本留存与按版本 pin 为后续项（TASK-6）：
    当前 ``meta.algorithm_registry`` 仍按 ``algorithm_id`` 单行登记。
    """

    def __init__(self) -> None:
        self._specs: dict[tuple[str, int], AlgorithmSpec] = {}

    def add(self, spec: AlgorithmSpec) -> AlgorithmSpec:
        key = (spec.algorithm_id, spec.version)
        existing = self._specs.get(key)
        if existing is None:
            self._specs[key] = spec
            return spec
        if existing == spec:
            return existing
        raise ValueError(
            f"算法身份冲突：{spec.identity}"
            f"（已注册 {existing.implementation}，收到 {spec.implementation}）"
        )

    def get(self, algorithm_id: str, version: int | None = None) -> AlgorithmSpec | None:
        """按 ``(id, version)`` 取；``version=None`` 取最高版本。"""
        if version is not None:
            return self._specs.get((algorithm_id, version))
        candidates = [spec for (name, _v), spec in self._specs.items() if name == algorithm_id]
        if not candidates:
            return None
        return max(candidates, key=lambda spec: spec.version)

    def ids(self) -> list[str]:
        return sorted({name for name, _version in self._specs})

    def __contains__(self, algorithm_id: object) -> bool:
        return algorithm_id in self.ids()

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Iterator[AlgorithmSpec]:
        return iter([self._specs[key] for key in sorted(self._specs)])

    def validate(self) -> list[str]:
        """自检（CI 规则）：命名 / 版本 / 实现路径 / docstring 标记。"""
        errors: list[str] = []
        for spec in self:
            label = f"算法 {spec.identity}"
            if not ALGORITHM_ID_PATTERN.match(spec.algorithm_id):
                errors.append(f"{label}: algorithm_id 命名非法（应形如 xxx；兼容旧写法 xxx_v1）")
            if spec.version < 1:
                errors.append(f"{label}: version 必须 >= 1")
            if not IMPLEMENTATION_PATTERN.match(spec.implementation):
                errors.append(f"{label}: implementation 非点分路径 {spec.implementation!r}")
            actual = implementation_path(spec.function)
            if actual != spec.implementation:
                errors.append(
                    f"{label}: implementation 与实现不一致（登记 {spec.implementation}，"
                    f"实际 {actual}）"
                )
            expected_version = version_from_id(spec.algorithm_id)
            if expected_version is not None and expected_version != spec.version:
                errors.append(
                    f"{label}: version={spec.version} 与 id 后缀 v{expected_version} 不一致"
                )
            docstring = spec.docstring
            missing = [marker for marker in DOCSTRING_MARKERS if marker not in docstring]
            if missing:
                errors.append(f"{label}: docstring 缺少标记 {missing}（doc-11 §4）")
        return errors


#: 默认注册表（模块导入即触发 ``@register`` 登记）
DEFAULT_REGISTRY = AlgorithmRegistry()


def implementation_path(function: Callable[..., Any]) -> str:
    """被装饰函数的真实实现路径（``module.qualname``；顶层函数才有意义）。"""
    return f"{function.__module__}.{function.__qualname__}"


def version_from_id(algorithm_id: str) -> int | None:
    """从 ``algorithm_id`` 后缀解析版本（``pe_ttm_v2`` → 2）。"""
    match = _VERSION_PATTERN.search(algorithm_id)
    return int(match.group(1)) if match else None


def build_spec(
    function: Callable[..., Any],
    *,
    algorithm_id: str,
    version: int | None = None,
    owner: str = "derived-engine",
    effective_from: date | None = None,
    reason: str = "",
    inline_sql: str | None = None,
) -> AlgorithmSpec:
    """由函数与元数据构造 :class:`AlgorithmSpec`（命名/版本结构性校验）。"""
    if not ALGORITHM_ID_PATTERN.match(algorithm_id):
        raise ValueError(
            f"algorithm_id 命名非法（应形如 xxx；兼容旧写法 xxx_v1）：{algorithm_id!r}"
        )
    parsed = version_from_id(algorithm_id)
    if version is None:
        if parsed is None:
            raise ValueError(
                f"algorithm_id={algorithm_id!r} 需显式 version（稳定 id 约定；或沿用 xxx_v1 后缀）"
            )
        version = parsed
    if parsed is not None and parsed != version:
        raise ValueError(f"version={version} 与 algorithm_id 后缀 v{parsed} 不一致：{algorithm_id}")
    if version < 1:
        raise ValueError(f"version 必须 >= 1：{algorithm_id}")
    path = implementation_path(function)
    if "<locals>" in path:
        raise ValueError(f"算法实现必须是模块级函数（不可为局部函数）：{path}")
    return AlgorithmSpec(
        algorithm_id=algorithm_id,
        version=version,
        owner=owner,
        implementation=path,
        function=function,
        effective_from=effective_from,
        reason=reason,
        inline_sql=inline_sql,
    )


def register(
    *,
    algorithm_id: str,
    version: int | None = None,
    owner: str = "derived-engine",
    effective_from: date | None = None,
    reason: str = "",
    inline_sql: str | None = None,
    registry: AlgorithmRegistry | None = None,
) -> Callable[[F], F]:
    """算法实现装饰器（doc-11 §4）：登记 ``algorithm_id`` 与升级台账元数据。"""

    def wrap(function: F) -> F:
        target = registry if registry is not None else DEFAULT_REGISTRY
        target.add(
            build_spec(
                function,
                algorithm_id=algorithm_id,
                version=version,
                owner=owner,
                effective_from=effective_from,
                reason=reason,
                inline_sql=inline_sql,
            )
        )
        return function

    return wrap
