"""Runtime 编排：按角色启动 Scheduler / Dispatcher / WorkerPool（doc-20 §3.2）。"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.config import RuntimeConfig
from fin_data_platform.runtime.health import ReadinessReport, readiness
from fin_data_platform.runtime.models import JobIntent
from fin_data_platform.runtime.registry import TaskRegistry, TaskSpec
from fin_data_platform.runtime.repository import MetaRepository, SqlMetaRepository
from fin_data_platform.runtime.roles import (
    Dispatcher,
    DueProvider,
    Scheduler,
    WorkerPool,
)


def _parse_schedule(schedule: str):
    """schedule 表达式：``interval:<秒>``（测试/近实时）或标准 cron（5 段）。"""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    text = schedule.strip()
    if text.startswith("interval:"):
        return IntervalTrigger(seconds=float(text.split(":", 1)[1]))
    return CronTrigger.from_crontab(text, timezone="UTC")


class RuntimeApp:
    """Runtime 骨架：任务注册镜像、角色编排、健康与就绪检查。

    - ``role=all``：Scheduler + Dispatcher + WorkerPool 同进程；
    - ``role=scheduler``：仅调度与分发（可独立进程）；
    - ``role=worker``：仅执行（可独立进程）；三者仅经 PostgreSQL 协调。
    """

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        engine: Engine | None = None,
        repository: MetaRepository | None = None,
        registry: TaskRegistry | None = None,
        due_provider: DueProvider | None = None,
    ) -> None:
        if repository is None and engine is None:
            raise ValueError("需要 engine 或 repository 之一")
        self._config = config
        self._engine = engine
        self._registry = registry or TaskRegistry()
        self._due_provider = due_provider
        if repository is not None:
            self._repo: MetaRepository = repository
        else:
            assert engine is not None
            self._repo = SqlMetaRepository(engine)
        self._scheduler = Scheduler(self._registry, due_provider=due_provider)
        self._dispatcher = Dispatcher(self._repo, max_queued=config.max_queued)
        self._pool = WorkerPool(
            self._repo,
            self._registry,
            # 进程级前缀：停机超时回收仅命中本进程的 running 行
            name=f"worker-{os.getpid()}",
            workers=config.worker_count,
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._aps: Any = None
        self._scheduler_backend = "polling"

    # ------------------------------------------------------------ 生命周期
    def sync_metadata(self) -> None:
        """镜像任务定义与依赖到 ``meta.*``（注册校验失败即拒绝启动）。"""
        errors = self._registry.validate()
        if errors:
            raise ValueError("任务注册校验失败: " + "; ".join(errors))
        self._repo.sync_defs(self._registry.defs())
        self._repo.sync_dependencies(self._registry.dependencies())

    def start(self) -> None:
        self._stop.clear()
        self.sync_metadata()
        role = self._config.role
        scheduled = [spec for spec in self._registry if spec.schedule]
        if role in ("all", "scheduler"):
            if scheduled:
                self._start_scheduler(scheduled)
            unscheduled = {spec.job_id for spec in self._registry if not spec.schedule}
            if unscheduled:
                thread = threading.Thread(
                    target=self._scheduler.run,
                    args=(self._stop, self._dispatcher.submit),
                    kwargs={
                        "interval": self._config.tick_interval,
                        "only": unscheduled,
                    },
                    name="runtime-scheduler",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
        if role in ("all", "worker"):
            self._pool.start(self._stop, interval=self._config.worker_interval)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._aps is not None:
            from fin_data_platform.runtime import triggers

            for job in self._aps.get_jobs():
                triggers.unregister_handler(job.id)
            self._aps.shutdown(wait=False)
            self._aps = None
            self._scheduler_backend = "polling"
        self._scheduler.health.alive = False
        self._pool.stop(timeout=timeout)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    # ------------------------------------------------------------ 调度（APScheduler）
    def _start_scheduler(self, specs: list[TaskSpec]) -> None:
        """APScheduler 定时（doc-20 §4.4）：PG job store 持久化调度注册。"""
        from apscheduler.jobstores.memory import MemoryJobStore
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from apscheduler.schedulers.background import BackgroundScheduler

        from fin_data_platform.runtime import triggers

        jobstore = (
            SQLAlchemyJobStore(engine=self._engine)
            if self._engine is not None
            else MemoryJobStore()
        )
        self._aps = BackgroundScheduler(jobstores={"default": jobstore}, timezone="UTC")
        for spec in specs:
            triggers.register_handler(spec.job_id, self._on_trigger)
            self._aps.add_job(
                triggers.fire,
                trigger=_parse_schedule(spec.schedule or ""),
                args=[spec.job_id],
                id=spec.job_id,
                name=spec.job_id,
                replace_existing=True,
            )
        self._aps.start()
        self._scheduler_backend = "apscheduler"
        # 清理已下线的陈旧调度注册（否则会按 cron 空转并污染 aps_jobs 计数）
        known = {spec.job_id for spec in specs}
        for job in self._aps.get_jobs():
            if job.id not in known:
                triggers.unregister_handler(job.id)
                self._aps.remove_job(job.id)
        # 启动即追平一次（cron 下一次触发前不空等；幂等由 Dispatcher/写入端保证）
        for spec in specs:
            self._run_spec(spec)

    def _on_trigger(self, job_id: str) -> None:
        try:
            spec = self._registry.get(job_id)
        except KeyError:  # 注册表移除但 store 仍有条目：忽略
            return
        self._run_spec(spec)

    def _run_spec(self, spec: TaskSpec) -> list[str]:
        """按到期窗口提交意图（无 due_provider 时空转）；异常写入健康输出。"""
        self._scheduler.health.alive = True
        self._scheduler.health.last_tick_at = utcnow()
        try:
            if spec.window_provider is not None:
                windows = list(spec.window_provider(utcnow()))
            elif self._due_provider is not None:
                windows = list(self._due_provider(spec, utcnow()))
            else:
                return []
            results = [
                self._dispatcher.submit(
                    self._registry.intent(spec, window_start=window_start, window_end=window_end)
                )
                for window_start, window_end in windows
            ]
        except Exception as exc:
            self._scheduler.health.last_error = f"{type(exc).__name__}: {exc}"
            return []
        self._scheduler.health.intents_emitted += len(results)
        return results

    # ------------------------------------------------------------ 提交与查询
    @property
    def registry(self) -> TaskRegistry:
        """任务注册表（只读用途：查询任务定义 / 构造意图）。"""
        return self._registry

    def submit(self, intent: JobIntent) -> str:
        return self._dispatcher.submit(intent)

    def tick(self, now: datetime | None = None) -> list[JobIntent]:
        return self._scheduler.tick(now)

    def run_pending(self) -> int:
        """同步执行一轮可领取任务（测试 / 手动运维用）。"""
        executed = 0
        while self._pool.execute_once() is not None:
            executed += 1
        return executed

    # ------------------------------------------------------------ 健康
    def health(self) -> dict[str, Any]:
        return {
            "role": self._config.role,
            "scheduler": {
                "backend": self._scheduler_backend,
                "alive": self._scheduler.health.alive,
                "last_tick_at": self._scheduler.health.last_tick_at,
                "intents_emitted": self._scheduler.health.intents_emitted,
                "last_error": self._scheduler.health.last_error,
                "aps_jobs": len(self._aps.get_jobs()) if self._aps is not None else 0,
            },
            "dispatcher": {
                "last_dispatch_at": self._dispatcher.health.last_dispatch_at,
                "created": self._dispatcher.health.created,
                "duplicate": self._dispatcher.health.duplicate,
                "dependency_blocked": self._dispatcher.health.dependency_blocked,
                "backpressure_blocked": self._dispatcher.health.backpressure_blocked,
            },
            "workers": {
                "alive": self._pool.health.alive,
                "threads_alive": self._pool.alive_threads(),
                "busy": self._pool.health.busy,
                "executed": self._pool.health.executed,
                "last_completed_at": self._pool.health.last_completed_at,
                "last_error": self._pool.health.last_error,
            },
        }

    def readiness(self) -> ReadinessReport | None:
        if self._engine is None:
            return None
        return readiness(
            self._engine,
            dsn=self._config.storage.write_dsn,
            check_directory=self._config.check_dictionary,
            check_schema=self._config.check_schema,
        )


def default_registry() -> TaskRegistry:
    """默认任务注册表（骨架期为空；任务由 TASK-3.5 / 3.6 / 3.12 等注册）。"""
    return TaskRegistry()
