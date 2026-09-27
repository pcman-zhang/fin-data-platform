"""源侧读取测试（TASK-3.32 步骤③）：三态 / 最新状态版本 / 禁 SQL JOIN。"""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.registry.store import EntityStore
from fin_data_platform.source import (
    STATUS_MISSING,
    STATUS_OK,
    STATUS_SUSPENDED,
    SourceReadError,
    UnknownEntity,
    read_bars_with_status,
)
from fin_data_platform.storage.schema import build_metadata

CODE = "600519.SH"
CODE_B = "000001.SZ"
D1 = date(2026, 9, 10)  # 交易日：有行情
D2 = date(2026, 9, 11)  # 交易日：停牌（无行情）
D3 = date(2026, 9, 14)  # 交易日：ST + 行情
D4 = date(2026, 9, 15)  # 交易日：无行情、无状态 → 真缺失
D5 = date(2026, 9, 12)  # 周六：非交易日（无行）
D6 = date(2026, 9, 16)  # 交易日：盘中停牌（状态停牌但有行情）
WINDOW_START = "2026-09-10"
WINDOW_END = "2026-09-16"
TS = pd.Timestamp("2026-09-16 08:00:00")


class FakeHub:
    def __init__(self, rows: list[dict] | None = None, source_attr: str | None = "tushare") -> None:
        self._rows = rows or []
        self._source_attr = source_attr
        self.calls: list[tuple] = []

    def get_bars(
        self,
        codes,
        *,
        start: str,
        end: str,
        freq: str = "1d",
        adjust: str | None = None,
        source=None,
        **_: object,
    ) -> pd.DataFrame:
        self.calls.append((tuple(codes), start, end, freq, adjust, source))
        frame = pd.DataFrame(self._rows)
        if self._source_attr is not None:
            frame.attrs["source"] = self._source_attr
        return frame


def _bar(day: date, code: str = CODE, close: float = 10.5) -> dict:
    return {
        "code": code,
        "date": pd.Timestamp(day),
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
    }


@pytest.fixture()
def engine():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        for schema in ("cn_equity", "cn_fund", "ref", "meta", "mart"):
            connection.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    metadata, _ = build_metadata()
    metadata.create_all(engine)
    calendar = metadata.tables["ref.trade_calendar"]
    days = {
        D1: True,
        D2: True,
        D5: False,
        date(2026, 9, 13): False,
        D3: True,
        D4: True,
        D6: True,
    }
    with engine.begin() as connection:
        connection.execute(
            calendar.insert(),
            [
                {
                    "exchange_id": "XSHG",
                    "trade_date": day,
                    "is_open": is_open,
                    "pretrade_date": None,
                    "knowledge_time": pd.Timestamp("2026-01-01"),
                    "version": 1,
                }
                for day, is_open in days.items()
            ],
        )
    return engine, metadata


def _entity(engine, code: str = CODE) -> int:  # type: ignore[no-untyped-def]
    return EntityStore(engine).ensure_entity(code=code, name="测试").entity_id


def _status(  # type: ignore[no-untyped-def]
    engine,
    metadata,
    entity_id: int,
    day: date,
    *,
    suspended: bool = False,
    st: bool = False,
    version: int = 1,
    knowledge_time: pd.Timestamp = TS,
) -> None:
    table = metadata.tables["cn_equity.daily_status"]
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            {
                "entity_id": entity_id,
                "trade_date": day,
                "is_suspended": suspended,
                "is_st": st,
                "knowledge_time": knowledge_time,
                "ingest_time": knowledge_time,
                "provider": "tushare",
                "version": version,
            },
        )


def test_three_states_and_non_trading_days(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    entity_id = _entity(db)
    _status(db, metadata, entity_id, D2, suspended=True)
    _status(db, metadata, entity_id, D3, st=True)
    _status(db, metadata, entity_id, D6, suspended=True)  # 盘中停牌：无全天行情但当日有 bar
    hub = FakeHub([_bar(D1), _bar(D3, close=11.0), _bar(D6, close=12.0)])

    result = read_bars_with_status(
        db, hub, CODE, start=WINDOW_START, end=WINDOW_END
    )
    frame = result.frame
    assert list(frame.columns) == [
        "code",
        "trade_date",
        "status",
        "is_suspended",
        "is_st",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert list(frame["trade_date"]) == [D1, D2, D3, D4, D6]  # 非交易日 D5 无行
    assert list(frame["status"]) == [
        STATUS_OK,
        STATUS_SUSPENDED,
        STATUS_OK,
        STATUS_MISSING,
        STATUS_OK,
    ]
    assert list(frame["is_st"]) == [False, False, True, False, False]
    assert list(frame["is_suspended"]) == [False, True, False, False, True]
    # 停牌 / 缺失行保留但数值为 NaN（不填充）
    assert frame.loc[1, ["open", "high", "low", "close", "volume", "amount"]].isna().all()
    assert frame.loc[3, ["open", "high", "low", "close", "volume", "amount"]].isna().all()
    assert frame.loc[4, "close"] == 12.0  # 盘中停牌以 bar 存在为准 → ok
    assert result.meta.trading_days == 5
    assert (result.meta.row_count, result.meta.ok_rows) == (5, 3)
    assert (result.meta.suspended_rows, result.meta.missing_rows) == (1, 1)
    assert result.meta.provider == "tushare"
    assert hub.calls == [(("600519.SH",), WINDOW_START, WINDOW_END, "1d", None, None)]


def test_latest_status_version_wins(engine) -> None:  # type: ignore[no-untyped-def]
    db, metadata = engine
    entity_id = _entity(db)
    # 版本 2 先写入、版本 1 后写入且知识时间相同：必须按 version 取最新（停牌）
    _status(db, metadata, entity_id, D2, suspended=True, version=2)
    _status(db, metadata, entity_id, D2, suspended=False, version=1)
    # ST 修订：v2（更晚知识时间）撤销 ST
    _status(db, metadata, entity_id, D3, st=True, version=1)
    _status(
        db,
        metadata,
        entity_id,
        D3,
        st=False,
        version=2,
        knowledge_time=pd.Timestamp("2026-09-17 08:00:00"),
    )
    hub = FakeHub([_bar(D2), _bar(D3)])

    frame = read_bars_with_status(db, hub, CODE, start=WINDOW_START, end=WINDOW_END).frame
    # D2 停牌标志来自 v2；但当日有 bar → ok（以 bar 为准），is_suspended 仍为 True
    assert frame.loc[frame["trade_date"] == D2, "is_suspended"].item() is True
    assert frame.loc[frame["trade_date"] == D2, "status"].item() == STATUS_OK
    # D3 最新版本已撤销 ST
    assert frame.loc[frame["trade_date"] == D3, "is_st"].item() is False


def test_unknown_entity_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    with pytest.raises(UnknownEntity) as excinfo:
        read_bars_with_status(db, FakeHub(), "600000.SH", start=WINDOW_START, end=WINDOW_END)
    assert excinfo.value.code == "unknown_entity"
    assert "未注册实体" in str(excinfo.value)


def test_invalid_window_raises(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    with pytest.raises(SourceReadError, match="窗口非法"):
        read_bars_with_status(db, FakeHub(), CODE, start=WINDOW_END, end=WINDOW_START)


def test_no_trading_days_skips_source_call(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    _entity(db)
    hub = FakeHub()
    result = read_bars_with_status(db, hub, CODE, start="2026-09-12", end="2026-09-13")
    assert result.frame.empty
    assert result.meta.trading_days == 0
    assert (result.meta.ok_rows, result.meta.suspended_rows, result.meta.missing_rows) == (0, 0, 0)
    assert hub.calls == []  # 空窗口不触发源调用


def test_multi_code_expected_grid(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    _entity(db, CODE)
    _entity(db, CODE_B)
    hub = FakeHub([_bar(D1, code=CODE), _bar(D2, code=CODE_B, close=9.0)])

    frame = read_bars_with_status(
        db, hub, [CODE, CODE_B], start=WINDOW_START, end=WINDOW_END
    ).frame
    assert len(frame) == 10  # 2 标的 × 5 交易日
    by_key = {(row.code, row.trade_date): row.status for row in frame.itertuples()}
    assert by_key[(CODE, D1)] == STATUS_OK
    assert by_key[(CODE, D2)] == STATUS_MISSING  # 无状态行、无行情
    assert by_key[(CODE_B, D2)] == STATUS_OK
    assert by_key[(CODE_B, D1)] == STATUS_MISSING


def test_read_does_not_use_sql_join(engine) -> None:  # type: ignore[no-untyped-def]
    db, _metadata = engine
    _entity(db)
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(db, "before_cursor_execute", _capture)
    try:
        read_bars_with_status(db, FakeHub([_bar(D1)]), CODE, start=WINDOW_START, end=WINDOW_END)
    finally:
        event.remove(db, "before_cursor_execute", _capture)
    assert statements, "读路径应执行单表 SELECT"
    assert not [item for item in statements if re.search(r"\bJOIN\b", item, re.I)]


def test_source_package_has_no_sql_join_literals() -> None:
    """静态扫描：源侧读取模块不得出现含 JOIN 的 SQL 字符串字面量。"""
    root = Path(__file__).resolve().parents[1] / "src" / "fin_data_platform" / "source"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and re.search(r"\bselect\b", node.value, re.I)
                and re.search(r"\bjoin\b", node.value, re.I)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"发现 SQL JOIN 字面量: {offenders}"
