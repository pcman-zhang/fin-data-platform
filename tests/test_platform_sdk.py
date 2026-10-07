"""FinDataPlatform SDK（TASK-3.11）单测：双模式 / 元数据同构 / 错误映射 / 兼容校验 / 配置注入。"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, insert, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.registry.reader import RegistryReader
from fin_data_platform.runtime.repository import SqlMetaRepository
from fin_data_platform.sdk import (
    FinDataError,
    FinDataPlatform,
    SdkConfig,
    SdkMode,
    check_schema_revision,
    export_json_schema,
)
from fin_data_platform.sdk.compat import MAX_SCHEMA_REVISION, MIN_SCHEMA_REVISION
from fin_data_platform.sdk.direct import DirectBackend
from fin_data_platform.sdk.rest import RestBackend
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

NOW = datetime(2026, 10, 1, 10, 0)
#: 知识时间早于 as_of（SQLite 的字符串时间比较不支持相等边界；PG 支持，见集成测试）
EARLIER = datetime(2026, 10, 1, 8, 0)
D0, D1 = date(2026, 9, 29), date(2026, 9, 30)
DATASET = "cn_equity.daily_bar"


def _bar(entity_id: int, day: date, close: float) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
        "amount": None,
        "knowledge_time": EARLIER,
        "publish_time": None,
        "ingest_time": EARLIER,
        "version": 1,
        "provider": "tushare",
    }


def _entity(entity_id: int, code: str, name: str, since: date) -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "equity",
        "market": "cn",
        "code": code,
        "name": name,
        "valid_from": since,
        "knowledge_time": EARLIER,
        "version": 1,
    }


def _calendar(day: date) -> dict:
    return {
        "exchange_id": "XSHG",
        "trade_date": day,
        "is_open": True,
        "knowledge_time": EARLIER,
        "version": 1,
    }


def _status(entity_id: int, day: date) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "is_suspended": False,
        "is_st": False,
        "knowledge_time": EARLIER,
        "ingest_time": EARLIER,
        "version": 1,
        "provider": "tushare",
    }


def _factor(entity_id: int, day: date) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "adj_factor": 1.0,
        "knowledge_time": EARLIER,
        "ingest_time": EARLIER,
        "version": 1,
        "provider": "tushare",
    }


def _seed(engine, metadata):  # type: ignore[no-untyped-def]
    def insert_rows(table: str, rows: list[dict]) -> None:
        with engine.begin() as connection:
            connection.execute(insert(metadata.tables[table]), rows)

    insert_rows(
        "ref.entity",
        [
            _entity(1, "600519.SH", "贵州茅台", date(2001, 8, 27)),
            _entity(2, "000858.SZ", "五粮液", date(2001, 1, 1)),
        ],
    )
    insert_rows("ref.trade_calendar", [_calendar(day) for day in (D0, D1)])
    insert_rows(
        DATASET,
        [_bar(1, D0, 100.0), _bar(1, D1, 102.0), _bar(2, D0, 200.0)],
    )
    insert_rows(
        "cn_equity.daily_status",
        [_status(1, D0), _status(1, D1), _status(2, D0)],
    )
    insert_rows(
        "cn_equity.adj_factor",
        [_factor(1, D0), _factor(1, D1)],
    )
    # SDK 连接校验读取 alembic_version（与迁移后的真实库一致）
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": MAX_SCHEMA_REVISION},
        )


@pytest.fixture()
def env():  # type: ignore[no-untyped-def]
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
    _seed(engine, metadata)
    repository = SqlMetaRepository(engine)
    context = ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=repository,
        algorithms=InMemoryAlgorithmStore(),
        registry=RegistryReader(engine),
        specs=load_all(),
        factors=None,
    )
    app = create_app(context, web_dist=None)
    return engine, app


def _direct(engine) -> FinDataPlatform:  # type: ignore[no-untyped-def]
    config = SdkConfig(mode=SdkMode.DIRECT, dsn="sqlite://")
    return FinDataPlatform(
        config=config, backend=DirectBackend(config, engine=engine, control_engine=engine)
    )


def _rest(app) -> FinDataPlatform:  # type: ignore[no-untyped-def]
    config = SdkConfig(mode=SdkMode.REST, rest_url="http://testserver")
    return FinDataPlatform(
        config=config, backend=RestBackend(config, client=TestClient(app))
    )


# ---------------------------------------------------------------- 配置与兼容
def test_config_from_env_and_validation() -> None:
    config = SdkConfig.from_env(
        {
            "FDP_SDK_MODE": "direct",
            "DATABASE_READ_USER": "app_ro",
            "DATABASE_READ_PASSWORD": "secret",
            "DATABASE_HOST": "db",
            "DATABASE_PORT": "5433",
            "DATABASE_NAME": "fdp",
        }
    )
    assert config.mode == SdkMode.DIRECT
    assert config.dsn is not None and "app_ro:secret@db:5433/fdp" in config.dsn
    # 显式 FDP_SDK_DSN 优先
    explicit = SdkConfig.from_env(
        {"FDP_SDK_MODE": "direct", "FDP_SDK_DSN": "postgresql+psycopg://u:p@h/db"}
    )
    assert explicit.dsn == "postgresql+psycopg://u:p@h/db"
    # rest 模式不要求 dsn
    rest = SdkConfig.from_env({"FDP_SDK_MODE": "rest", "FDP_SDK_REST_URL": "http://x:9000"})
    assert rest.mode == SdkMode.REST and rest.rest_url == "http://x:9000"
    # direct 模式缺 dsn → 校验失败；配置冻结
    with pytest.raises(ValidationError):
        SdkConfig(mode=SdkMode.DIRECT)
    with pytest.raises(ValidationError):
        config.dsn = "other"  # type: ignore[misc]


def test_compat_check() -> None:
    check_schema_revision(MAX_SCHEMA_REVISION)
    check_schema_revision(MIN_SCHEMA_REVISION)
    with pytest.raises(FinDataError) as old:
        check_schema_revision("0005_reference_data")
    assert old.value.code == "schema_incompatible" and "过旧" in old.value.detail
    with pytest.raises(FinDataError) as new:
        check_schema_revision("0009_future")
    assert new.value.code == "schema_incompatible" and "较新" in new.value.detail
    with pytest.raises(FinDataError):
        check_schema_revision(None)
    schema = export_json_schema()
    assert set(schema) == {"ResultMeta", "FactorResultMeta", "ControlRunInfo", "ErrorModel"}
    assert "properties" in schema["ResultMeta"]


# ---------------------------------------------------------------- 直连模式
def test_direct_mode_reads(env) -> None:  # type: ignore[no-untyped-def]
    engine, _app = env
    fdp = _direct(engine).connect()

    bars = fdp.raw.read(
        DATASET,
        fields=["entity_id", "trade_date", "close"],
        entities=[1],
        window=(D0, D1),
        adjust="raw",
        as_of=NOW,
    )
    assert bars.frame["close"].tolist() == [100.0, 102.0]
    assert bars.meta.adjust == "raw" and bars.meta.semantic_version == 1
    assert bars.table is not None and bars.table.num_rows == 2

    rows = fdp.read_model.read(
        DATASET, version_mode="latest", fields=["entity_id", "close"], order_by=["entity_id"]
    )
    assert rows.frame["close"].tolist() == [100.0, 102.0, 200.0]
    assert rows.meta.version_mode == "latest" and rows.meta.row_count == 3

    series = fdp.panel.get_series(
        DATASET,
        fields=["close"],
        start=D0,
        end=D1,
        as_of=NOW,
        entities=[1],
        adjust="raw",
    )
    assert not series.frame.empty
    joined = fdp.panel.asof_join(
        pd.DataFrame({"entity_id": [1], "trade_date": [D1]}),
        pd.DataFrame({"entity_id": [1], "trade_date": [D0], "value": [9.9]}),
        left_on="trade_date",
        by="entity_id",
    )
    assert joined["value"].tolist() == [9.9]

    with pytest.raises(FinDataError) as exc:
        fdp.raw.read("nope", as_of=NOW)
    assert exc.value.code == "invalid_dataset"
    with pytest.raises(FinDataError) as factor:
        fdp.factors.read("ma20", as_of=NOW)
    assert factor.value.code == "factor_not_materialized"


def test_direct_control_requires_control_dsn(env) -> None:  # type: ignore[no-untyped-def]
    engine, _app = env
    config = SdkConfig(mode=SdkMode.DIRECT, dsn="sqlite://")
    fdp = FinDataPlatform(
        config=config, backend=DirectBackend(config, engine=engine, control_engine=None)
    )
    with pytest.raises(FinDataError) as exc:
        fdp.control.trigger("sync.reference.market_registry")
    assert exc.value.code == "control_unavailable"
    # 有 control 引擎但任务未注册 → job_not_registered（结构化）
    fdp2 = _direct(engine)
    with pytest.raises(FinDataError) as missing:
        fdp2.control.trigger("sync.nope")
    assert missing.value.code == "job_not_registered"


def _records(frame: pd.DataFrame) -> list[dict]:
    """帧值归一化（日期/时间 → ISO 字符串；REST 返回 JSON 类型，直连返回原生类型）。"""
    clean = frame.astype(object).where(frame.notna(), None)
    records = clean.to_dict("records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, (datetime, date)):
                record[key] = value.isoformat()
    return records


# ---------------------------------------------------------------- REST 模式
def test_rest_mode_parity_and_errors(env) -> None:  # type: ignore[no-untyped-def]
    engine, app = env
    direct = _direct(engine).connect()
    rest = _rest(app).connect()

    fields = ["entity_id", "trade_date", "close"]
    direct_rows = direct.read_model.read(
        DATASET, version_mode="latest", fields=fields, order_by=["entity_id"]
    )
    rest_rows = rest.read_model.read(
        DATASET, version_mode="latest", fields=fields, order_by=["entity_id"]
    )
    assert _records(direct_rows.frame) == _records(rest_rows.frame)
    assert direct_rows.meta.model_dump() == rest_rows.meta.model_dump()

    direct_raw = direct.raw.read(
        DATASET, fields=fields, entities=[1], window=(D0, D1), adjust="raw", as_of=NOW
    )
    rest_raw = rest.raw.read(
        DATASET, fields=fields, entities=[1], window=(D0, D1), adjust="raw", as_of=NOW
    )
    assert _records(direct_raw.frame) == _records(rest_raw.frame)
    assert direct_raw.meta.model_dump() == rest_raw.meta.model_dump()

    with pytest.raises(FinDataError) as exc:
        rest.raw.read("nope", as_of=NOW)
    assert exc.value.code == "invalid_dataset"  # RFC 9457 → 结构化 code
    with pytest.raises(FinDataError) as factor:
        rest.factors.read("ma20", as_of=NOW)
    assert factor.value.code in ("factor_not_materialized", "unknown_factor")
    with pytest.raises(FinDataError) as panel:
        rest.panel.get_series(DATASET, fields=["close"], start=D0, end=D1, as_of=NOW)
    assert panel.value.code == "unsupported_in_rest_mode"
    with pytest.raises(FinDataError) as codes:
        rest.control.ensure(DATASET)
    assert codes.value.code == "invalid_request"
    with pytest.raises(FinDataError) as unregistered:
        rest.control.ensure(DATASET, codes=["600519.SH"])
    assert unregistered.value.code == "job_not_registered"
    with pytest.raises(FinDataError) as trigger:
        rest.control.trigger("sync.nope")
    assert trigger.value.code == "job_not_registered"


# ---------------------------------------------------------------- 评审修复回归（REST 后端 / 配置）
class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """RestBackend 注入桩：按路径返回载荷；可注入传输错误。"""

    def __init__(self, payloads: dict[str, object], *, error: Exception | None = None) -> None:
        self.payloads = payloads
        self.error = error
        self.requests: list[tuple[str, str, object, object]] = []

    def request(
        self, method: str, path: str, *, params: object = None, json: object = None
    ) -> _FakeResponse:
        self.requests.append((method, path, params, json))
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payloads.get(path))

    def close(self) -> None:
        pass


def _rest_backend(client: FakeClient) -> RestBackend:
    return RestBackend(SdkConfig(mode=SdkMode.REST, rest_url="http://stub"), client=client)


def test_rest_transport_error_mapping() -> None:
    import httpx

    backend = _rest_backend(FakeClient({}, error=httpx.ConnectError("boom")))
    with pytest.raises(FinDataError) as exc:
        backend.check()
    assert exc.value.code == "upstream_unavailable"


def test_rest_compat_uses_schema_revision() -> None:
    backend = _rest_backend(
        FakeClient(
            {
                "/v1/health": {
                    "ok": True,
                    "checks": {"schema_revision": True},
                    "errors": [],
                    "schema_revision": "0009_future",
                }
            }
        )
    )
    with pytest.raises(FinDataError) as exc:
        backend.check()
    assert exc.value.code == "schema_incompatible"


def test_rest_trigger_window_rejected() -> None:
    client = FakeClient({})
    backend = _rest_backend(client)
    with pytest.raises(FinDataError) as exc:
        backend.control_trigger("quality.scan", window=(D0, D1), request_id=None)
    assert exc.value.code == "unsupported_in_rest_mode"
    assert client.requests == []  # 未发出请求


def test_rest_wait_preserves_created_and_note() -> None:
    backend = _rest_backend(
        FakeClient(
            {
                "/v1/jobs/5": {
                    "run_id": 5,
                    "job_id": "sync.cn_equity.daily_bar.600519.SH",
                    "dataset": DATASET,
                    "kind": "sync",
                    "status": "succeeded",
                    "window_start": D0.isoformat(),
                    "window_end": D1.isoformat(),
                }
            }
        )
    )
    info = backend._wait_run(5, 5.0, created=False, note="命中既有运行（幂等）")
    assert info.created is False and info.note == "命中既有运行（幂等）"
    with pytest.raises(FinDataError) as exc:
        backend._wait_run(0, 1.0)
    assert exc.value.code == "invalid_request"


def test_config_dsn_escaping_and_read_endpoint() -> None:
    config = SdkConfig.from_env(
        {
            "FDP_SDK_MODE": "direct",
            "DATABASE_READ_USER": "app_ro",
            "DATABASE_READ_PASSWORD": "p/a ss@x",
            "DATABASE_READ_HOST": "ro.db",
            "DATABASE_READ_PORT": "5433",
            "DATABASE_READ_NAME": "ro_name",
        }
    )
    assert config.dsn is not None
    assert "app_ro:p%2Fa%20ss%40x@ro.db:5433/ro_name" in config.dsn


def test_sdk_context_manager_and_limits(env) -> None:  # type: ignore[no-untyped-def]
    engine, app = env
    with _direct(engine) as fdp:
        limited = fdp.raw.read(
            DATASET, fields=["entity_id", "close"], entities=[1], adjust="raw", as_of=NOW, limit=1
        )
        assert len(limited.frame) == 1
        assert any("limit" in warning for warning in limited.meta.warnings)
    with _rest(app) as rest:
        rows = rest.raw.read(
            DATASET, fields=["entity_id", "close"], entities=[1], adjust="raw", as_of=NOW, limit=1
        )
        assert len(rows.frame) == 1


def test_empty_frame_columns_parity(env) -> None:  # type: ignore[no-untyped-def]
    engine, app = env
    direct = _direct(engine)
    rest = _rest(app)
    fields = ["entity_id", "trade_date", "close"]
    empty_direct = direct.raw.read(
        DATASET, fields=fields, entities=[99999], adjust="raw", as_of=NOW
    )
    empty_rest = rest.raw.read(
        DATASET, fields=fields, entities=[99999], adjust="raw", as_of=NOW
    )
    assert len(empty_direct.frame) == 0 and len(empty_rest.frame) == 0
    assert list(empty_direct.frame.columns) == fields
    assert list(empty_rest.frame.columns) == fields


def test_filters_validation_both_modes(env) -> None:  # type: ignore[no-untyped-def]
    engine, app = env
    bad = [{"field": "close"}]  # 缺 op
    with pytest.raises(FinDataError) as direct_exc:
        _direct(engine).read_model.read(DATASET, version_mode="latest", filters=bad)
    assert direct_exc.value.code == "unsupported_filter"
    with pytest.raises(FinDataError) as rest_exc:
        _rest(app).read_model.read(DATASET, version_mode="latest", filters=bad)
    assert rest_exc.value.code == "unsupported_filter"


def test_ensure_prechecks_no_side_effects(env) -> None:  # type: ignore[no-untyped-def]
    engine, _app = env
    fdp = _direct(engine)
    with pytest.raises(FinDataError) as multi:
        fdp.control.ensure(DATASET, codes=["600519.SH", "000858.SZ"])
    assert multi.value.code == "invalid_request"
    with pytest.raises(FinDataError) as missing:
        fdp.control.ensure(DATASET, codes=["600519.SH"])
    assert missing.value.code == "job_not_registered"
    with engine.connect() as connection:
        runs = connection.execute(text("SELECT count(*) FROM meta.job_runs")).scalar_one()
    assert runs == 0  # 前置校验：未产生任何运行
