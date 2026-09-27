"""控制面意图客户端（TASK-3.26）：ensure / materialize + wait。

定位：回填与物化的**唯一入口**——库接口（后续 SDK 于 TASK-3.11 薄封装；REST 为
同一实现的薄封装）。客户端只提交**意图**（写 ``meta.job_runs`` 队列），数据写入
一律由 Runtime 执行；客户端不存在数据写路径（doc-21 §1）。

口径：

- **幂等**：``request_id`` 命中既有运行（且请求一致）直接返回；否则按 ``job_key``
  查既有运行（可领取 / 运行中 / 已成功视为同一次意图），``created=False``；
  ``request_id`` 仅支持单代码提交（多代码请分别提交或省略）；
- **窗口缺省**：``end = 最近已收盘交易日``（落库日历 + 16:30 CST 截止；日历不可用
  时放宽为今天 UTC）；``start`` 缺省取各 scope 水位 + 1（无水位时为 ``end``，即单日）；
- **窗口校验**：显式窗口终点晚于最近已收盘 → :class:`InvalidWindow`（不静默截断）；
  已追平（水位 + 1 > end）的任务不提交（返回空 :class:`ControlRuns`）。
"""

from __future__ import annotations

import time as time_module
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import Engine

from fin_data_platform.control.errors import (
    IntentError,
    IntentTimeout,
    InvalidRequest,
    InvalidWindow,
    JobNotRegistered,
    UnknownFactor,
)
from fin_data_platform.derived.store import AlgorithmStore, SqlAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.dictionary.models import DatasetSpec, DerivedEntry
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.calendar import StoredTradeCalendar, TradeCalendar
from fin_data_platform.runtime.keys import job_key as build_job_key
from fin_data_platform.runtime.keys import job_scope
from fin_data_platform.runtime.models import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    JobIntent,
    JobKind,
    JobRun,
    JobStatus,
)
from fin_data_platform.runtime.repository import MetaRepository, SqlMetaRepository

#: 等待即返回的状态：终态 + 不再被领取的 failed / interrupted
_WAIT_DONE = frozenset(
    TERMINAL_STATUSES | {JobStatus.FAILED.value, JobStatus.INTERRUPTED.value}
)


@dataclass(frozen=True, slots=True)
class ControlRun:
    """意图运行句柄（只读；``wait`` 轮询到终态）。"""

    run_id: int
    job_id: str
    dataset: str
    kind: str
    status: str
    window_start: date | None
    window_end: date | None
    version_dimension: str | None
    _repository: MetaRepository = field(repr=False, compare=False)
    created: bool = True
    request_id: str | None = None
    #: 命中方式：``None``（新建）| ``request_id``（幂等键命中）| ``job_key``（同窗口去重）
    matched_via: str | None = None
    _poll_interval: float = field(default=0.2, repr=False, compare=False)
    _sleep: Callable[[float], None] = field(default=time_module.sleep, repr=False, compare=False)

    @classmethod
    def from_run(
        cls,
        run: JobRun,
        *,
        created: bool,
        repository: MetaRepository,
        poll_interval: float,
        sleep: Callable[[float], None],
        request_id: str | None = None,
        matched_via: str | None = None,
    ) -> ControlRun:
        return cls(
            run_id=run.run_id,
            job_id=run.job_id,
            dataset=run.dataset,
            kind=run.kind,
            status=run.status,
            window_start=run.window_start,
            window_end=run.window_end,
            version_dimension=run.version_dimension,
            created=created,
            request_id=request_id or run.request_id,
            matched_via=matched_via,
            _repository=repository,
            _poll_interval=poll_interval,
            _sleep=sleep,
        )

    @property
    def done(self) -> bool:
        return self.status in _WAIT_DONE

    def refresh(self) -> ControlRun:
        """重新读取运行状态（返回新句柄）。"""
        run = self._repository.get_run(self.run_id)
        if run is None:
            raise IntentError(f"运行不存在: {self.run_id}", hint="检查 run_id 或元数据存储")
        return ControlRun.from_run(
            run,
            created=False,
            repository=self._repository,
            poll_interval=self._poll_interval,
            sleep=self._sleep,
        )

    def wait(self, timeout: float | None = None) -> JobRun:
        """等待到达终态（或 failed / interrupted）；超时抛 :class:`IntentTimeout`。"""
        return _wait_all([self], timeout=timeout)[0]


@dataclass(frozen=True, slots=True)
class ControlRuns:
    """多个运行句柄（ensure 多代码场景；``wait`` 等待全部）。"""

    runs: tuple[ControlRun, ...]

    def __iter__(self) -> Iterator[ControlRun]:
        return iter(self.runs)

    def __len__(self) -> int:
        return len(self.runs)

    def __bool__(self) -> bool:
        return bool(self.runs)

    @property
    def created(self) -> tuple[ControlRun, ...]:
        return tuple(run for run in self.runs if run.created)

    def wait(self, timeout: float | None = None) -> list[JobRun]:
        """等待全部到达终态；返回各运行的最新记录（顺序与提交一致）。"""
        return _wait_all(list(self.runs), timeout=timeout)


def _wait_all(handles: Sequence[ControlRun], *, timeout: float | None) -> list[JobRun]:
    if not handles:
        return []
    deadline = None if timeout is None else time_module.monotonic() + timeout
    pending = list(handles)
    done: dict[int, JobRun] = {}
    while pending:
        still: list[ControlRun] = []
        for handle in pending:
            run = handle._repository.get_run(handle.run_id)
            if run is None:
                raise IntentError(
                    f"运行不存在: {handle.run_id}", hint="检查 run_id 或元数据存储"
                )
            if run.status in _WAIT_DONE:
                done[run.run_id] = run
            else:
                still.append(handle)
        pending = still
        if pending:
            if deadline is not None and time_module.monotonic() >= deadline:
                ids = ", ".join(str(handle.run_id) for handle in pending)
                raise IntentTimeout(
                    f"等待超时：运行 {ids} 未达终态（当前 {pending[0].status}）",
                    hint="任务继续在平台侧执行；可用 run_id 查询状态",
                )
            handles[0]._sleep(handles[0]._poll_interval)
    return [done[handle.run_id] for handle in handles]


class ControlClient:
    """控制面意图客户端（ensure / materialize / run + wait）。"""

    def __init__(
        self,
        engine: Engine,
        *,
        specs: Mapping[str, DatasetSpec] | None = None,
        meta: MetaRepository | None = None,
        algorithms: AlgorithmStore | None = None,
        calendar: TradeCalendar | None = None,
        clock: Callable[[], datetime] = utcnow,
        poll_interval: float = 0.2,
        sleep: Callable[[float], None] = time_module.sleep,
        exchange: str = "XSHG",
    ) -> None:
        self._meta = meta if meta is not None else SqlMetaRepository(engine)
        self._algorithms = algorithms if algorithms is not None else SqlAlgorithmStore(engine)
        self._specs = {
            name: spec for name, spec in (specs if specs is not None else load_all()).items()
        }
        self._calendar = (
            calendar
            if calendar is not None
            else StoredTradeCalendar(engine, exchange=exchange, clock=clock)
        )
        self._clock = clock
        self._poll_interval = poll_interval
        self._sleep = sleep

    # ---------------------------------------------------------------- 查询
    def last_closed(self, now: datetime | None = None) -> date | None:
        """最近已收盘交易日（落库日历；不可用时 ``None``）。"""
        return self._calendar.last_closed(now or self._clock())

    def run(self, run_id: int) -> ControlRun:
        """查询既有运行的句柄。"""
        run = self._meta.get_run(run_id)
        if run is None:
            raise IntentError(f"运行不存在: {run_id}", hint="检查 run_id 或元数据存储")
        return self._handle(run, created=False)

    # ---------------------------------------------------------------- 意图
    def ensure(
        self,
        dataset: str,
        *,
        codes: Sequence[str] | None = None,
        window: tuple[date, date] | None = None,
        request_id: str | None = None,
        priority: int = 100,
    ) -> ControlRuns:
        """提交采集/回填意图（``sync.<dataset>.<code>``；幂等）。

        ``codes`` 缺省 = 该数据集全部已注册同步任务的 scope；已追平的代码不提交
        （返回空 ``ControlRuns``）。
        """
        registered = self._registered_sync_codes(dataset)
        if not registered:
            raise JobNotRegistered(
                f"未注册同步任务: {dataset}",
                hint="检查数据集名与同步装配（FDP_SYNC_CODES）",
            )
        targets = list(dict.fromkeys(codes)) if codes is not None else sorted(registered)
        unknown = [code for code in targets if code not in registered]
        if unknown:
            raise JobNotRegistered(
                f"代码未注册同步任务: {unknown}",
                hint=f"已注册: {sorted(registered)}",
            )
        if not targets:
            raise JobNotRegistered(f"{dataset}: 没有可提交的代码", hint="检查代码清单")
        if request_id is not None and len(targets) > 1:
            raise InvalidRequest(
                f"request_id 仅支持单代码提交（收到 {len(targets)} 个代码）",
                hint="多代码请分别提交，或省略 request_id（仍按同窗口 job_key 去重）",
            )

        end = self.last_closed() or self._clock().date()
        windows: dict[str, tuple[date, date]] = {}
        if window is not None:
            start, explicit_end = window
            if start > explicit_end:
                raise InvalidWindow(
                    f"窗口为空: {start} > {explicit_end}", hint="修正窗口起止顺序"
                )
            if explicit_end > end:
                raise InvalidWindow(
                    f"窗口终点 {explicit_end} 晚于最近已收盘交易日 {end}",
                    hint="改用不晚于最近已收盘的终点，或等待收盘后重试",
                )
            windows = {code: (start, explicit_end) for code in targets}
        else:
            for code in targets:
                mark = self._meta.get_watermark(dataset, scope=code)
                start = (
                    mark.watermark_time.date() + timedelta(days=1)
                    if mark is not None and mark.watermark_time is not None
                    else end
                )
                if start > end:
                    continue  # 已追平：不提交
                windows[code] = (start, end)
        if not windows:
            return ControlRuns(())

        runs = tuple(
            self._submit(
                kind=JobKind.SYNC.value,
                job_id=f"sync.{dataset}.{code}",
                dataset=dataset,
                scope=code,
                window=windows[code],
                version_dimension=None,
                request_id=request_id,
                priority=priority,
            )
            for code in targets
            if code in windows
        )
        return ControlRuns(runs)

    def materialize(
        self,
        factor: str,
        *,
        dataset: str | None = None,
        request_id: str | None = None,
        priority: int = 150,
    ) -> ControlRun:
        """提交因子物化意图（``derive.<dataset>.<output>``；幂等）。

        窗口 = 触发日（与调度窗口提供者一致，保证同键幂等）；版本维度 = 算法身份
        ``id@vN``（来自算法台账）。
        """
        target, entry = self._resolve_factor(factor, dataset=dataset)
        job_id = f"derive.{target}.{entry.output}"
        registered = {
            definition.job_id
            for definition in self._meta.list_defs()
            if definition.kind == JobKind.DERIVE.value
        }
        if job_id not in registered:
            raise JobNotRegistered(
                f"物化任务未注册: {job_id}",
                hint="该因子可能为 materialize=none（按需计算），或未随 Runtime 装配",
            )
        identity = self._algorithm_identity(target, entry)
        day = self._clock().date()
        return self._submit(
            kind=JobKind.DERIVE.value,
            job_id=job_id,
            dataset=target,
            scope="",
            window=(day, day),
            version_dimension=identity,
            request_id=request_id,
            priority=priority,
        )

    # ------------------------------------------------------------ 内部实现
    def trigger(
        self,
        job_id: str,
        *,
        window: tuple[date, date] | None = None,
        request_id: str | None = None,
    ) -> ControlRun:
        """按任务标识触发**全局同步任务**（窗口缺省 = 触发日；幂等）。

        仅支持 ``scope=""`` 的 sync 任务（如全市场登记任务）；按代码任务请用
        :meth:`ensure`（按水位生成窗口），物化任务请用 :meth:`materialize`。
        """
        definition = next(
            (item for item in self._meta.list_defs() if item.job_id == job_id), None
        )
        if definition is None:
            raise JobNotRegistered(f"任务未注册: {job_id}", hint="检查任务标识或装配配置")
        if definition.kind == JobKind.DERIVE.value:
            raise IntentError(
                f"{job_id} 为物化任务，请使用 materialize 提交意图",
                hint="control.materialize(<factor>)",
            )
        if definition.kind != JobKind.SYNC.value:
            raise IntentError(
                f"暂不支持触发 kind={definition.kind} 的任务",
                hint="可选 sync（全局任务）/ derive（materialize）",
            )
        if job_scope(job_id, definition.dataset):
            raise IntentError(
                f"按代码任务请使用 ensure（按水位生成窗口）：{job_id}",
                hint="ensure(dataset, codes=[...])",
            )
        day = self._clock().date()
        start, end = window or (day, day)
        if start > end:
            raise InvalidWindow(f"窗口为空: {start} > {end}", hint="修正窗口起止顺序")
        if end > day:
            raise InvalidWindow(
                f"窗口终点不得晚于今天（{day}）：{end}", hint="全局任务使用触发日窗口"
            )
        return self._submit(
            kind=definition.kind,
            job_id=job_id,
            dataset=definition.dataset,
            scope="",
            window=(start, end),
            version_dimension=None,
            request_id=request_id,
            priority=definition.priority,
        )

    def _handle(
        self,
        run: JobRun,
        *,
        created: bool,
        request_id: str | None = None,
        matched_via: str | None = None,
    ) -> ControlRun:
        return ControlRun.from_run(
            run,
            created=created,
            repository=self._meta,
            poll_interval=self._poll_interval,
            sleep=self._sleep,
            request_id=request_id,
            matched_via=matched_via,
        )

    def _registered_sync_codes(self, dataset: str) -> set[str]:
        prefix = f"sync.{dataset}."
        return {
            definition.job_id[len(prefix) :]
            for definition in self._meta.list_defs()
            if definition.kind == JobKind.SYNC.value
            and definition.job_id.startswith(prefix)
        }

    def _resolve_factor(
        self, factor: str, *, dataset: str | None
    ) -> tuple[str, DerivedEntry]:
        if dataset is not None:
            spec = self._specs.get(dataset)
            if spec is None:
                raise UnknownFactor(f"数据集不存在: {dataset}", hint="检查数据集名")
            matches = [entry for entry in spec.derived or [] if entry.output == factor]
            if not matches:
                raise UnknownFactor(
                    f"数据集 {dataset} 无因子输出: {factor}",
                    hint=f"可用: {sorted(entry.output for entry in spec.derived or [])}",
                )
            return dataset, matches[0]

        if "." in factor:
            target, _, output = factor.rpartition(".")
            spec = self._specs.get(target)
            if spec is None:
                raise UnknownFactor(f"数据集不存在: {target}", hint="检查 dataset.output 拼写")
            matches = [entry for entry in spec.derived or [] if entry.output == output]
            if not matches:
                raise UnknownFactor(
                    f"数据集 {target} 无因子输出: {output}",
                    hint=f"可用: {sorted(entry.output for entry in spec.derived or [])}",
                )
            return target, matches[0]

        found = [
            (name, entry)
            for name, spec in sorted(self._specs.items())
            for entry in spec.derived or []
            if entry.output == factor
        ]
        if not found:
            raise UnknownFactor(
                f"因子不存在: {factor}", hint="检查因子名，或用 dataset.output 显式限定"
            )
        if len(found) > 1:
            candidates = sorted(f"{name}.{entry.output}" for name, entry in found)
            raise UnknownFactor(
                f"因子重名（需显式 dataset）: {factor}", hint=f"候选: {candidates}"
            )
        return found[0][0], found[0][1]

    def _algorithm_identity(self, dataset: str, entry: DerivedEntry) -> str:
        rows = [
            row
            for row in self._algorithms.list_all()
            if row.dataset == dataset and row.output == entry.output
        ]
        if not rows:
            raise IntentError(
                f"算法台账缺少 {dataset}.{entry.output}（{entry.algorithm_id}）",
                hint="运行 Runtime 同步元数据（sync_metadata / derived 同步）后再提交",
            )
        latest = max(rows, key=lambda row: row.version)
        return f"{latest.algorithm_id}@v{latest.version}"

    def _submit(
        self,
        *,
        kind: str,
        job_id: str,
        dataset: str,
        scope: str,
        window: tuple[date, date],
        version_dimension: str | None,
        request_id: str | None,
        priority: int,
    ) -> ControlRun:
        start, end = window
        if request_id:
            existing = self._meta.find_run_by_request_id(request_id)
            if existing is not None:
                # 幂等键语义：同一逻辑请求返回既有运行（不校验载荷一致；
                # 返回句柄携带原始窗口与状态，调用方可自行比对）
                return self._handle(
                    existing, created=False, request_id=request_id, matched_via="request_id"
                )

        key = build_job_key(
            kind=kind,
            job_id=job_id,
            scope=scope,
            window_start=start,
            window_end=end,
            version_dimension=version_dimension,
        )
        # 与 create_run 的去重口径一致：可领取 / 运行中 / 已成功视为同一次意图；
        # 失败（dead/failed/interrupted）不拦截——重复提交即重试
        active = CLAIMABLE_STATUSES | {
            JobStatus.RUNNING.value,
            JobStatus.SUCCEEDED.value,
        }
        existing = self._meta.find_run_by_job_key(key)
        if existing is not None and existing.status in active:
            return self._handle(
                existing, created=False, request_id=request_id, matched_via="job_key"
            )

        run = self._meta.create_run(
            JobIntent(
                kind=kind,
                job_id=job_id,
                dataset=dataset,
                scope=scope,
                window_start=start,
                window_end=end,
                version_dimension=version_dimension,
                priority=priority,
            ),
            request_id=request_id,
        )
        if run is None:
            raise IntentError(
                f"同窗口意图已存在（{job_id} {start}~{end}）",
                hint="查询既有运行状态，或改用 request_id 幂等提交",
            )
        return self._handle(run, created=True, request_id=request_id)
