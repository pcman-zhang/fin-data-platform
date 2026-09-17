"""任务声明式注册（doc-20 §4.1）。

任务定义 = 元数据（kind / dataset / 调度 / 重试 / 优先级 / 依赖）+ 执行器；
注册后镜像到 ``meta.job_defs`` / ``meta.job_dependencies``，执行状态一律在
``meta.job_runs``（不依赖进程内状态）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from fin_data_platform.runtime.keys import VERSIONED_KINDS
from fin_data_platform.runtime.models import (
    DependencyCondition,
    JobDef,
    JobDependency,
    JobIntent,
    JobRun,
    JobStatus,
)

if TYPE_CHECKING:
    from fin_data_platform.runtime.repository import MetaRepository

#: 执行器：接收运行上下文，返回执行结果
JobExecutor = Callable[["JobContext"], "JobResult"]


@dataclass(frozen=True, slots=True)
class JobResult:
    rows_written: int = 0


@dataclass(frozen=True, slots=True)
class JobContext:
    """执行上下文：任务定义 + 本次运行（窗口 / 版本维度 / 幂等键）。"""

    job_id: str
    kind: str
    dataset: str
    run: JobRun

    @property
    def scope(self) -> str:
        return self.run.scope

    @property
    def window_start(self):
        return self.run.window_start

    @property
    def window_end(self):
        return self.run.window_end

    @property
    def version_dimension(self) -> str | None:
        return self.run.version_dimension


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """任务定义（声明式）。"""

    job_id: str
    kind: str
    dataset: str
    executor: JobExecutor
    schedule: str | None = None
    priority: int = 100
    max_attempts: int = 3
    scope: str = ""
    dependencies: tuple[str, ...] = ()
    condition: str = DependencyCondition.ON_SUCCESS.value
    #: 版本维度提供者：derive → algorithm_id；build_rm → 读模型 semantic_version
    version_provider: Callable[[], str | None] | None = None
    #: 窗口提供者（无水位语义的任务，如派生物化：按触发时间给窗口，随时间推进幂等维度）
    window_provider: Callable[[datetime], list[tuple[date, date]]] | None = None
    #: 成功回调（如更新水位）：``(context, result, repository) -> None``
    on_success: Callable[[JobContext, JobResult, MetaRepository], None] | None = None


class TaskRegistry:
    """任务注册表（注册即声明；``validate()`` 做依赖 / 版本维度一致性检查）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskSpec] = {}

    def register(self, spec: TaskSpec) -> TaskSpec:
        if spec.job_id in self._tasks:
            raise ValueError(f"任务重复注册: {spec.job_id}")
        self._tasks[spec.job_id] = spec
        return spec

    def task(
        self,
        job_id: str,
        *,
        kind: str,
        dataset: str,
        schedule: str | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        scope: str = "",
        dependencies: tuple[str, ...] = (),
        condition: str = DependencyCondition.ON_SUCCESS.value,
        version_provider: Callable[[], str | None] | None = None,
        on_success: Callable[[JobContext, JobResult, MetaRepository], None] | None = None,
    ) -> Callable[[JobExecutor], JobExecutor]:
        """装饰器形式注册任务。"""

        def decorator(executor: JobExecutor) -> JobExecutor:
            self.register(
                TaskSpec(
                    job_id=job_id,
                    kind=kind,
                    dataset=dataset,
                    executor=executor,
                    schedule=schedule,
                    priority=priority,
                    max_attempts=max_attempts,
                    scope=scope,
                    dependencies=dependencies,
                    condition=condition,
                    version_provider=version_provider,
                    on_success=on_success,
                )
            )
            return executor

        return decorator

    def get(self, job_id: str) -> TaskSpec:
        if job_id not in self._tasks:
            raise KeyError(f"任务未注册: {job_id}")
        return self._tasks[job_id]

    def __iter__(self) -> Iterator[TaskSpec]:
        return iter(self._tasks.values())

    def specs(self) -> list[TaskSpec]:
        return list(self._tasks.values())

    def defs(self) -> list[JobDef]:
        return [
            JobDef(
                job_id=spec.job_id,
                kind=spec.kind,
                dataset=spec.dataset,
                schedule=spec.schedule,
                priority=spec.priority,
                max_attempts=spec.max_attempts,
            )
            for spec in self._tasks.values()
        ]

    def dependencies(self) -> list[JobDependency]:
        return [
            JobDependency(
                parent_job=parent,
                child_job=spec.job_id,
                condition=spec.condition,
            )
            for spec in self._tasks.values()
            for parent in spec.dependencies
        ]

    def validate(self) -> list[str]:
        """校验：依赖存在、无环、版本维度合法。"""
        errors: list[str] = []
        for spec in self._tasks.values():
            for parent in spec.dependencies:
                if parent not in self._tasks:
                    errors.append(f"{spec.job_id}: 依赖任务未注册 {parent}")
            if spec.kind in VERSIONED_KINDS and spec.version_provider is None:
                label = VERSIONED_KINDS[spec.kind]
                errors.append(f"{spec.job_id}: {spec.kind} 必须提供 version_provider（{label}）")
            if spec.kind not in VERSIONED_KINDS and spec.version_provider is not None:
                errors.append(
                    f"{spec.job_id}: {spec.kind} 不应提供 version_provider"
                    "（仅 derive / build_rm 版本感知）"
                )
        errors.extend(self._validate_acyclic())
        return errors

    def intent(self, spec: TaskSpec, *, window_start, window_end) -> JobIntent:
        """按任务定义构造调度意图（版本维度由 provider 求值）。"""
        version = spec.version_provider() if spec.version_provider else None
        return JobIntent(
            kind=spec.kind,
            job_id=spec.job_id,
            dataset=spec.dataset,
            scope=spec.scope,
            window_start=window_start,
            window_end=window_end,
            version_dimension=version,
            priority=spec.priority,
            max_attempts=spec.max_attempts,
        )

    def _validate_acyclic(self) -> list[str]:
        errors: list[str] = []
        state: dict[str, int] = {}

        def visit(node: str, path: list[str]) -> None:
            state[node] = 1
            for parent in self._tasks[node].dependencies:
                if parent not in self._tasks:
                    continue
                if state.get(parent) == 1:
                    errors.append(f"依赖成环: {' -> '.join([*path, node, parent])}")
                elif state.get(parent) is None:
                    visit(parent, [*path, node])
            state[node] = 2

        for job_id in sorted(self._tasks):
            if state.get(job_id) is None:
                visit(job_id, [])
        return errors


def is_claimable(status: str) -> bool:
    """该状态是否可被 WorkerPool 领取。"""
    return status in (JobStatus.QUEUED.value, JobStatus.RETRYING.value)
