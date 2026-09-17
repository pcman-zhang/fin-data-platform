"""派生任务挂载（doc-20 §2：Derived Engine Executor）。

- 仅 ``materialize=latest`` 的派生注册 ``derive`` 任务；``version_dimension=algorithm_id``
  （幂等键随算法升级变化；旧 run 与旧实现永久留存，可审计回溯）；
- ``refresh=scheduled`` 挂 cron（``FDP_DERIVE_SCHEDULE``），否则仅手动 / API 触发；
- ``materialize=none`` 不注册任务：按需计算由引擎承担，读模型内联走 ``inline_sql``；
- 物化为**全量重算**（as_of=now）：可重复执行，成功后写 ``meta.data_generation`` 并按域失效缓存；
- 调度窗口 = **触发日**（``window_provider``）：无水位语义的任务需要一个随时间推进的幂等维度，
  否则 ``job_key`` 恒定、第二次起会被 ``create_run`` 当作既有运行静默去重（同日重复触发同键、
  次日触发为新运行）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from fin_data_platform.derived.engine import DerivedEngine
from fin_data_platform.derived.registry import AlgorithmRegistry
from fin_data_platform.derived.store import AlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec, DerivedEntry
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.models import JobKind
from fin_data_platform.runtime.registry import (
    JobContext,
    JobExecutor,
    JobResult,
    TaskRegistry,
    TaskSpec,
)

if TYPE_CHECKING:
    from fin_data_platform.cache import LayeredCache


def register_derived_tasks(
    registry: TaskRegistry,
    engine: Engine,
    *,
    specs: Mapping[str, DatasetSpec] | None = None,
    registry_algorithms: AlgorithmRegistry | None = None,
    store: AlgorithmStore | None = None,
    schedule: str | None = None,
    priority: int = 150,
    cache: LayeredCache | None = None,
) -> list[TaskSpec]:
    """注册 ``latest`` 物化任务（返回已注册任务；一致性失败即抛错）。"""
    dictionary = specs if specs is not None else load_all()
    derived_engine = DerivedEngine(
        engine, specs=dictionary, registry=registry_algorithms, store=store
    )
    registered: list[TaskSpec] = []
    for dataset, spec in sorted(dictionary.items()):
        for entry in spec.derived or []:
            if entry.materialize.value != "latest":
                continue
            scheduled = schedule if entry.refresh.value == "scheduled" else None
            registered.append(
                registry.register(
                    TaskSpec(
                        job_id=f"derive.{dataset}.{entry.output}",
                        kind=JobKind.DERIVE.value,
                        dataset=dataset,
                        executor=_materialize_executor(derived_engine, dataset, entry, cache),
                        schedule=scheduled,
                        priority=priority,
                        scope=entry.output,
                        version_provider=_version_provider(entry),
                        window_provider=daily_window_provider,
                    )
                )
            )
    return registered


def _version_provider(entry: DerivedEntry) -> Callable[[], str | None]:
    return lambda: entry.algorithm_id


def daily_window_provider(now: datetime) -> list[tuple[date, date]]:
    """派生物化窗口 = 触发日（同日重复触发同键幂等；次日触发为新运行）。"""
    day = now.date()
    return [(day, day)]


def _materialize_executor(
    derived_engine: DerivedEngine,
    dataset: str,
    entry: DerivedEntry,
    cache: LayeredCache | None,
) -> JobExecutor:
    def executor(_context: JobContext) -> JobResult:
        report = derived_engine.materialize(entry.output, as_of=utcnow(), dataset=dataset)
        if cache is not None:
            cache.invalidate_domain(dataset.split(".", 1)[0])
        return JobResult(rows_written=report.rows)

    return executor
