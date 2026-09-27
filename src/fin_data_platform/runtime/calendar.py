"""交易日历适配（TASK-3.6 切片 2）：推导「最近已收盘交易日」（Hub / 落库两种实现）。

知识时间口径：A 股 15:00 收盘 = 07:00 UTC；但**调度判定**需等源端发布
（15:00–16:00 CST），故默认以 16:30 CST（08:30 UTC）为「今日已收盘」截止，
避免在发布前取到空数据并错误推进水位。

- :class:`HubTradeCalendar`：经 FinDataHub 取日历（Runtime 调度用）；
- :class:`StoredTradeCalendar`：读落库日历 ``ref.trade_calendar``（不依赖 Hub，
  控制面意图校验用；日历不可用时返回 ``None``，由调用方决定 fail-open 策略）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, Protocol

from sqlalchemy import Engine, inspect, select

from fin_data_platform.registry._util import to_date
from fin_data_platform.runtime._util import utcnow

#: 调度判定「今日已收盘」的截止（16:30 CST = 08:30 UTC）
_PUBLISH_CUTOFF_UTC = time(8, 30)


@lru_cache(maxsize=1)
def _calendar_table():  # type: ignore[no-untyped-def]
    """落库日历表（进程内缓存：build_metadata 开销较大；延迟导入防循环）。"""
    from fin_data_platform.storage.schema import build_metadata

    metadata, _specs = build_metadata()
    return metadata.tables["ref.trade_calendar"]


class TradeCalendar(Protocol):
    """日历协议（测试与 Hub 实现可互换）。"""

    def is_trading_day(self, day: date) -> bool: ...

    def last_closed(self, now: datetime) -> date | None: ...


class HubTradeCalendar:
    """Hub 日历实现（进程内缓存；按需向前回溯窗口加载）。"""

    def __init__(
        self,
        hub: Any,
        *,
        source: Any = None,
        lookback_days: int = 45,
        publish_cutoff_utc: time = _PUBLISH_CUTOFF_UTC,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._hub = hub
        self._source = source
        self._lookback = lookback_days
        self._publish_cutoff = publish_cutoff_utc
        self._clock = clock
        self._cache: dict[date, set[date]] = {}

    def _load(self, end: date) -> set[date]:
        """加载 ``[end-lookback, end]`` 的交易日集合（含缓存）。"""
        if end in self._cache:
            return self._cache[end]
        start = end - timedelta(days=self._lookback)
        frame = self._hub.get_trade_calendar(
            start=start.isoformat(), end=end.isoformat(), source=self._source
        )
        days: set[date] = set()
        if frame is not None and not frame.empty:
            for row in frame.to_dict("records"):
                day = to_date(row.get("date"))
                if day is not None and bool(row.get("is_open", True)):
                    days.add(day)
        self._cache[end] = days
        return days

    def is_trading_day(self, day: date) -> bool:
        return day in self._load(day)

    def last_closed(self, now: datetime | None = None) -> date | None:
        moment = now or self._clock()
        today = moment.date()
        closed_end = (
            today
            if moment.time() >= self._publish_cutoff
            else today - timedelta(days=1)
        )
        days = [day for day in self._load(closed_end) if day <= closed_end]
        return max(days) if days else None


class StoredTradeCalendar:
    """落库日历实现（``ref.trade_calendar``；不依赖 Hub，进程内缓存）。

    与 :class:`HubTradeCalendar` 同口径：16:30 CST（08:30 UTC）为「今日已收盘」截止。
    日历表缺失（未迁移）或窗口内无行（未导入/未覆盖）时视为不可用，``last_closed``
    返回 ``None``，由调用方按 fail-open 处理（控制面缺省窗口回退为今天）；
    其它数据库错误（连接失败等）正常抛出，不静默降级。
    """

    def __init__(
        self,
        engine: Engine,
        *,
        exchange: str = "XSHG",
        lookback_days: int = 45,
        publish_cutoff_utc: time = _PUBLISH_CUTOFF_UTC,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._engine = engine
        self._exchange = exchange
        self._lookback = lookback_days
        self._publish_cutoff = publish_cutoff_utc
        self._clock = clock
        self._cache: dict[date, set[date]] = {}

    def _available(self) -> bool:
        """日历表是否存在（未迁移 → False；连接失败等错误正常抛出）。"""
        return bool(inspect(self._engine).has_table("trade_calendar", schema="ref"))

    def _load(self, end: date) -> set[date]:
        """加载 ``[end-lookback, end]`` 的交易日集合（含缓存；表缺失 → 空集合）。"""
        if end in self._cache:
            return self._cache[end]
        if not self._available():
            self._cache[end] = set()
            return self._cache[end]
        start = end - timedelta(days=self._lookback)
        table = _calendar_table()
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(table.c.trade_date).where(
                        table.c.exchange_id == self._exchange,
                        table.c.trade_date >= start,
                        table.c.trade_date <= end,
                        table.c.is_open.is_(True),
                    )
                )
                .scalars()
                .all()
            )
        days = {day for row in rows if (day := to_date(row)) is not None}
        self._cache[end] = days
        return days

    def is_trading_day(self, day: date) -> bool:
        return day in self._load(day)

    def last_closed(self, now: datetime | None = None) -> date | None:
        moment = now or self._clock()
        today = moment.date()
        closed_end = (
            today
            if moment.time() >= self._publish_cutoff
            else today - timedelta(days=1)
        )
        days = [day for day in self._load(closed_end) if day <= closed_end]
        return max(days) if days else None
