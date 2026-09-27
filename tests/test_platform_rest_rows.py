"""REST 数据面（TASK-3.7 / doc-12）单测：PIT 行 / Raw / Factor / 元数据 / 传输 / SLO。"""

from __future__ import annotations

import time
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.registry.reader import RegistryReader
from fin_data_platform.runtime.repository import SqlMetaRepository
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

NOW = datetime(2026, 9, 25, 10, 0)
EARLY = datetime(2026, 9, 20, 10, 0)
D0, D1, D2 = date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)
DATASET = "cn_equity.daily_bar"
CODE_A, CODE_B = "600519.SH", "000858.SZ"


def _bar(
    entity_id: int,
    day: date,
    close: float,
    *,
    knowledge: datetime = NOW,
    version: int = 1,
    publish: datetime | None = None,
) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
        "amount": None,
        "knowledge_time": knowledge,
        "publish_time": publish,
        "ingest_time": knowledge,
        "version": version,
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
        "knowledge_time": NOW,
        "version": 1,
    }


def _calendar(day: date) -> dict:
    return {
        "exchange_id": "XSHG",
        "trade_date": day,
        "is_open": True,
        "knowledge_time": NOW,
        "version": 1,
    }


def _status(entity_id: int, day: date, *, suspended: bool = False) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "is_suspended": suspended,
        "is_st": False,
        "knowledge_time": NOW,
        "ingest_time": NOW,
        "version": 1,
        "provider": "tushare",
    }


def _factor(entity_id: int, day: date) -> dict:
    return {
        "entity_id": entity_id,
        "trade_date": day,
        "adj_factor": 1.0,
        "knowledge_time": NOW,
        "ingest_time": NOW,
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
            _entity(1, CODE_A, "贵州茅台", date(2001, 8, 27)),
            _entity(2, CODE_B, "五粮液", date(2001, 1, 1)),
        ],
    )
    insert_rows(
        "ref.entity_code_history",
        [
            {
                "entity_id": 1,
                "code": CODE_A,
                "valid_from": date(2001, 8, 27),
                "valid_to": None,
                "knowledge_time": NOW,
                "version": 1,
            }
        ],
    )
    insert_rows(
        "ref.entity_external_id",
        [
            {
                "entity_id": 1,
                "id_type": "isin",
                "id_value": "CN0000000001",
                "valid_from": date(2001, 8, 27),
                "valid_to": None,
                "knowledge_time": NOW,
                "version": 1,
            }
        ],
    )
    insert_rows("ref.trade_calendar", [_calendar(day) for day in (D0, D1, D2)])
    insert_rows(
        "cn_equity.daily_bar",
        [
            # entity 1：D0 两个版本（v2 修正）+ D1
            _bar(1, D0, 100.0, knowledge=EARLY, version=1, publish=EARLY),
            _bar(1, D0, 101.0, knowledge=NOW, version=2, publish=NOW),
            _bar(1, D1, 102.0, knowledge=NOW, version=1, publish=NOW),
            # entity 2：仅 D0（D1 用于对齐 missing / suspended 场景）
            _bar(2, D0, 200.0, knowledge=NOW, version=1, publish=NOW),
        ],
    )
    insert_rows(
        "cn_equity.daily_status",
        [
            _status(1, D0),
            _status(1, D1),
            _status(2, D0),
            _status(2, D1, suspended=True),
        ],
    )
    insert_rows(
        "cn_equity.adj_factor",
        [_factor(1, D0), _factor(1, D1)],
    )


@pytest.fixture()
def api():  # type: ignore[no-untyped-def]
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
    repository.set_watermark(DATASET, scope=CODE_A, watermark_time=NOW)
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
    client = TestClient(create_app(context, web_dist=None))
    return engine, metadata, repository, client


# ---------------------------------------------------------------- PIT 行
def test_rows_version_modes(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    latest = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest", "fields": "entity_id,trade_date,close"},
    )
    assert latest.status_code == 200
    rows = latest.json()["rows"]
    assert [(r["entity_id"], r["trade_date"], r["close"]) for r in rows] == [
        (1, D0.isoformat(), 101.0),  # v2 为最新版本
        (1, D1.isoformat(), 102.0),
        (2, D0.isoformat(), 200.0),
    ]
    assert latest.json()["meta"]["as_of"] is None
    assert latest.headers["x-version-mode"] == "latest"
    assert latest.headers["x-semantic-version"] == "1"
    assert latest.headers["x-query-rows"] == "3"

    as_of = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "as_of",
            "as_of": "2026-09-22T00:00:00",
            "fields": "entity_id,close",
            "entity_id": [1],
        },
    )
    assert as_of.status_code == 200
    assert as_of.json()["rows"] == [{"entity_id": 1, "close": 100.0}]  # 旧版本
    assert as_of.headers["x-as-of"].startswith("2026-09-22")

    history = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "history", "fields": "entity_id,close", "entity_id": [1]},
    )
    assert history.status_code == 200
    rows = history.json()["rows"]
    assert len(rows) == 3  # 全部版本
    assert {"version", "knowledge_time"} <= set(rows[0])


def test_rows_projection_filters_cursor(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    projected = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest", "fields": "entity_id,close"},
    )
    assert projected.json()["rows"][0] == {"entity_id": 1, "close": 101.0}

    with_meta = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest", "fields": "entity_id,version", "include_meta": True},
    )
    assert with_meta.json()["rows"][0] == {"entity_id": 1, "version": 2}

    filtered = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "fields": "entity_id,close",
            "filters": '[{"field":"close","op":"gte","value":150}]',
        },
    )
    assert [r["entity_id"] for r in filtered.json()["rows"]] == [2]

    paged = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest", "fields": "entity_id,trade_date,close", "limit": 2},
    )
    body = paged.json()
    assert len(body["rows"]) == 2 and body["meta"]["next_cursor"]
    second = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "fields": "entity_id,trade_date,close",
            "limit": 2,
            "cursor": body["meta"]["next_cursor"],
        },
    )
    assert [r["entity_id"] for r in second.json()["rows"]] == [2]
    assert second.json()["meta"]["next_cursor"] is None

    desc = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest", "fields": "entity_id,close", "order_by": "-entity_id"},
    )
    assert [r["entity_id"] for r in desc.json()["rows"]] == [2, 1, 1]


def test_rows_publish_policy(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    # daily_bar 有 publish_time：按发布时刻过滤（EARLY 时点看不到 v2）
    published = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "as_of",
            "as_of": "2026-09-22T00:00:00",
            "as_of_policy": "publish",
            "fields": "entity_id,close",
            "entity_id": [1],
        },
    )
    assert published.status_code == 200
    assert published.json()["rows"] == [{"entity_id": 1, "close": 100.0}]
    # adj_factor 无 publish_time：strict → 422；allow → 回退 knowledge 并标注
    strict = client.get(
        "/v1/datasets/cn_equity.adj_factor/rows",
        params={"version_mode": "as_of", "as_of": "2026-09-22T00:00:00", "as_of_policy": "publish"},
    )
    assert strict.status_code == 422
    assert strict.json()["title"] == "publish_time_missing"
    allowed = client.get(
        "/v1/datasets/cn_equity.adj_factor/rows",
        params={
            "version_mode": "as_of",
            "as_of": "2026-09-22T00:00:00",
            "as_of_policy": "publish",
            "fallback_mode": "allow",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["x-publish-fallback"] == "knowledge"
    assert allowed.json()["meta"]["fallback"] == "knowledge"
    assert allowed.json()["meta"]["warnings"]


def test_rows_errors(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    missing_mode = client.get(f"/v1/datasets/{DATASET}/rows")
    assert missing_mode.status_code == 422
    assert missing_mode.json()["title"] == "version_mode_required"
    assert missing_mode.json()["request_id"]

    bad_mode = client.get(f"/v1/datasets/{DATASET}/rows", params={"version_mode": "now"})
    assert bad_mode.status_code == 422 and bad_mode.json()["title"] == "invalid_version_mode"

    no_as_of = client.get(f"/v1/datasets/{DATASET}/rows", params={"version_mode": "as_of"})
    assert no_as_of.status_code == 422 and no_as_of.json()["title"] == "as_of_required"

    bad_as_of = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "as_of", "as_of": "yesterday"},
    )
    assert bad_as_of.status_code == 422 and bad_as_of.json()["title"] == "invalid_as_of"

    unknown = client.get("/v1/datasets/nope/rows", params={"version_mode": "latest"})
    assert unknown.status_code == 404 and unknown.json()["title"] == "invalid_dataset"

    bad_field = client.get(
        f"/v1/datasets/{DATASET}/rows", params={"version_mode": "latest", "fields": "nope"}
    )
    assert bad_field.status_code == 422 and bad_field.json()["title"] == "invalid_field"

    bad_op = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "filters": '[{"field":"close","op":"like","value":1}]',
        },
    )
    assert bad_op.status_code == 422 and bad_op.json()["title"] == "unsupported_filter"

    bad_cursor = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest", "cursor": "not-base64!"},
    )
    assert bad_cursor.status_code == 422 and bad_cursor.json()["title"] == "unsupported_filter"


def test_rows_transport_etag_gzip_arrow(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    params = {"version_mode": "latest", "fields": "entity_id,close"}
    first = client.get(f"/v1/datasets/{DATASET}/rows", params=params)
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    again = client.get(
        f"/v1/datasets/{DATASET}/rows", params=params, headers={"if-none-match": etag}
    )
    assert again.status_code == 304 and again.headers["etag"] == etag

    # 数据更新（水位推进）后 ETag 必须变化，否则客户端会拿到陈旧 304
    _repo.set_watermark(DATASET, scope=CODE_A, watermark_time=datetime(2026, 9, 26, 10, 0))
    changed = client.get(f"/v1/datasets/{DATASET}/rows", params=params)
    assert changed.headers["etag"] != etag
    assert changed.status_code == 200

    compressed = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "history", "include_meta": True},
        headers={"accept-encoding": "gzip"},
    )
    assert compressed.status_code == 200
    assert compressed.headers.get("content-encoding") == "gzip"

    arrow = client.get(
        f"/v1/datasets/{DATASET}/rows", params={**params, "format": "arrow"}
    )
    assert arrow.status_code == 200
    assert arrow.headers["content-type"] == "application/vnd.apache.arrow.stream"
    import pyarrow as pa

    table = pa.ipc.open_stream(arrow.content).read_all()
    assert table.num_rows == 3 and "close" in table.column_names


# ---------------------------------------------------------------- 访问面 Raw
def test_raw_rows_adjust_and_alignment(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    raw = client.get(
        f"/v1/raw/{DATASET}/rows",
        params={
            "as_of": "2026-09-26T00:00:00",
            "adjust": "raw",
            "entity_id": [1],
            "start": D0.isoformat(),
            "end": D1.isoformat(),
            "fields": "entity_id,trade_date,close",
        },
    )
    assert raw.status_code == 200
    assert raw.headers["x-adjust"] == "raw"
    assert [r["close"] for r in raw.json()["rows"]] == [101.0, 102.0]

    aligned = client.get(
        f"/v1/raw/{DATASET}/rows",
        params={
            "as_of": "2026-09-26T00:00:00",
            "adjust": "raw",
            "align_calendar": True,
            "entity_id": [1, 2],
            "start": D0.isoformat(),
            "end": D1.isoformat(),
            "fields": "entity_id,trade_date,close",
        },
    )
    assert aligned.status_code == 200
    rows = {(r["entity_id"], r["trade_date"]): r for r in aligned.json()["rows"]}
    assert rows[(1, D0.isoformat())]["status"] == "ok"
    assert rows[(2, D1.isoformat())]["status"] == "suspended"  # 停牌标注
    assert rows[(2, D1.isoformat())]["close"] is None  # 缺行不填充

    no_as_of = client.get(f"/v1/raw/{DATASET}/rows")
    assert no_as_of.status_code == 422 and no_as_of.json()["title"] == "as_of_required"

    bad_adjust = client.get(
        f"/v1/raw/{DATASET}/rows",
        params={"as_of": "2026-09-26T00:00:00", "adjust": "bogus"},
    )
    assert bad_adjust.status_code == 422 and bad_adjust.json()["title"] == "unsupported_adjust"


def test_factor_rows_strict_semantics(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    # latest 单份未物化：明确报错（不 lazy 回填）
    response = client.get(
        "/v1/factors/ma20/rows", params={"as_of": "2026-09-26T00:00:00"}
    )
    assert response.status_code == 404
    assert response.json()["title"] in ("factor_not_materialized", "unknown_factor")


# ---------------------------------------------------------------- 元数据 / 新鲜度
def test_schema_aliases_health(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    schema = client.get(f"/v1/datasets/{DATASET}/schema")
    assert schema.status_code == 200
    body = schema.json()
    assert body["title"] == DATASET
    assert body["properties"]["close"]["x-unit"] == "元"
    assert "trade_date" in body["required"]
    assert body["x-business-key"] == ["entity_id", "trade_date"]
    assert client.get("/v1/datasets/nope/schema").status_code == 404

    aliases = client.get("/v1/entities/1/aliases")
    assert aliases.status_code == 200
    body = aliases.json()
    assert body["code"] == CODE_A
    assert body["codes"][0]["code"] == CODE_A
    assert body["external_ids"][0]["id_value"] == "CN0000000001"
    assert client.get("/v1/entities/999/aliases").status_code == 404

    assert client.get("/v1/health").status_code == 200


def test_freshness_endpoint(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    response = client.get("/v1/freshness")
    assert response.status_code == 200
    items = {item["dataset"]: item for item in response.json()["datasets"]}
    assert DATASET in items
    assert items[DATASET]["watermark"] == NOW.date().isoformat()


# ---------------------------------------------------------------- SLO 基准（离线基线）
def test_rows_slo_benchmark(api) -> None:  # type: ignore[no-untyped-def]
    """小批量在线查询 P95 ≤ 500ms（离线 sqlite 基线；PG 集成另行测量）。"""
    _engine, _metadata, _repo, client = api
    params = {"version_mode": "latest", "fields": "entity_id,trade_date,close"}
    samples: list[float] = []
    for _ in range(30):
        start = time.perf_counter()
        response = client.get(f"/v1/datasets/{DATASET}/rows", params=params)
        samples.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    print(f"\nSLO 基线：p50={samples[len(samples) // 2]:.1f}ms p95={p95:.1f}ms")
    assert p95 < 500


def test_rows_etag_covers_query_params(api) -> None:  # type: ignore[no-untyped-def]
    """ETag 必须覆盖决定响应的全部参数（否则客户端会拿到错误 304）。"""
    _engine, _metadata, _repo, client = api
    base = {"version_mode": "latest", "fields": "entity_id,close"}

    def etag(**extra: object) -> str:
        response = client.get(f"/v1/datasets/{DATASET}/rows", params={**base, **extra})
        assert response.status_code == 200
        return response.headers["etag"]

    variants = [
        etag(entity_id=[1]),
        etag(entity_id=[2]),
        etag(limit=1),
        etag(limit=3),
        etag(start=D0.isoformat(), end=D1.isoformat()),
        etag(format="arrow"),
    ]
    assert len(set(variants)) == len(variants)
    # 分页游标参与 ETag
    first = client.get(
        f"/v1/datasets/{DATASET}/rows", params={**base, "limit": 1}
    )
    cursor = first.json()["meta"]["next_cursor"]
    page2 = client.get(
        f"/v1/datasets/{DATASET}/rows", params={**base, "limit": 1, "cursor": cursor}
    )
    assert page2.headers["etag"] != first.headers["etag"]


def test_rows_cursor_null_boundary(api) -> None:  # type: ignore[no-untyped-def]
    """可空排序列在边界行为 NULL 时不给游标（并给出提示），消费端不会 500。"""
    _engine, _metadata, _repo, client = api
    response = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "fields": "entity_id,amount",
            "order_by": "amount",
            "limit": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["next_cursor"] is None
    assert any("空值" in warning for warning in body["meta"]["warnings"])


def test_rows_filter_value_types(api) -> None:  # type: ignore[no-untyped-def]
    """过滤值类型非法 → 422（不把绑定错误留给数据库）。"""
    _engine, _metadata, _repo, client = api
    bad = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "filters": '[{"field":"entity_id","op":"eq","value":{"a":1}}]',
        },
    )
    assert bad.status_code == 422 and bad.json()["title"] == "unsupported_filter"

    coerced = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "fields": "entity_id,close",
            "filters": '[{"field":"entity_id","op":"eq","value":"2"}]',
        },
    )
    assert coerced.status_code == 200
    assert [row["entity_id"] for row in coerced.json()["rows"]] == [2]


def test_rows_is_null_and_format_and_projection(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    # is_null：缺省 true = IS NULL；false = IS NOT NULL
    is_null = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "fields": "entity_id,amount",
            "filters": '[{"field":"amount","op":"is_null"}]',
        },
    )
    assert is_null.status_code == 200 and len(is_null.json()["rows"]) == 3
    not_null = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "latest",
            "fields": "entity_id,amount",
            "filters": '[{"field":"amount","op":"is_null","value":false}]',
        },
    )
    assert not_null.status_code == 200 and not_null.json()["rows"] == []

    bad_format = client.get(
        f"/v1/datasets/{DATASET}/rows", params={"version_mode": "latest", "format": "xml"}
    )
    assert bad_format.status_code == 422 and bad_format.json()["title"] == "unsupported_filter"

    empty_projection = client.get(
        f"/v1/datasets/{DATASET}/rows", params={"version_mode": "latest", "fields": "version"}
    )
    assert empty_projection.status_code == 422
    assert empty_projection.json()["title"] == "invalid_field"


def test_rows_pit_class_gate_and_history_warning(api) -> None:  # type: ignore[no-untyped-def]
    _engine, _metadata, _repo, client = api
    scd2 = client.get("/v1/datasets/ref.entity/rows", params={"version_mode": "latest"})
    assert scd2.status_code == 422 and scd2.json()["title"] == "unsupported_pit_class"

    history = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "history", "as_of": "2026-09-22T00:00:00", "entity_id": [1]},
    )
    assert history.status_code == 200
    assert any("as_of 被忽略" in warning for warning in history.json()["meta"]["warnings"])


def test_rows_version_order_matches_access(api) -> None:  # type: ignore[no-untyped-def]
    """版本选择与访问面同口径（version 优先）：v2(早知识) 胜过 v1(晚知识)。"""
    engine, metadata, _repo, client = api
    with engine.begin() as connection:
        connection.execute(
            insert(metadata.tables[DATASET]),
            [
                _bar(3, D0, 99.0, knowledge=EARLY, version=2),
                _bar(3, D0, 10.0, knowledge=NOW, version=1),
            ],
        )
    response = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={
            "version_mode": "as_of",
            "as_of": "2026-09-22T00:00:00",
            "fields": "entity_id,close",
            "entity_id": [3],
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [{"entity_id": 3, "close": 99.0}]


def test_problem_json_and_request_id(api) -> None:  # type: ignore[no-untyped-def]
    """数据面错误体为 RFC 9457（application/problem+json）并回写 X-Request-Id。"""
    _engine, _metadata, _repo, client = api
    response = client.get(
        f"/v1/datasets/{DATASET}/rows",
        params={"version_mode": "latest"},
        headers={"x-request-id": "req-123"},
    )
    assert response.status_code == 200  # 正常请求不受影响
    missing = client.get(f"/v1/datasets/{DATASET}/rows")
    assert missing.status_code == 422
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.headers["x-request-id"]
    # 框架级参数校验（limit=0）在数据面也走问题体
    invalid = client.get(
        f"/v1/datasets/{DATASET}/rows", params={"version_mode": "latest", "limit": 0}
    )
    assert invalid.status_code == 422
    assert invalid.json()["title"] == "invalid_query"
    # 非数据面端点保持既有错误体
    other = client.get("/v1/datasets/nope")
    assert other.status_code == 404 and other.json()["title"] == "not_found"


def test_freshness_lists_all_datasets(api) -> None:  # type: ignore[no-untyped-def]
    """新鲜度列出数据集全集（未采集 / 未跑质量的 watermark=None）。"""
    _engine, _metadata, _repo, client = api
    response = client.get("/v1/freshness")
    assert response.status_code == 200
    items = {item["dataset"]: item for item in response.json()["datasets"]}
    assert DATASET in items and items[DATASET]["watermark"] == NOW.date().isoformat()
    assert "cn_equity.daily_status" in items
    assert items["cn_equity.daily_status"]["watermark"] is None


def test_factor_unknown_output(api) -> None:  # type: ignore[no-untyped-def]
    """未登记因子输出 → 结构化 404（不再 500）。"""
    _engine, _metadata, _repo, client = api
    response = client.get(
        "/v1/factors/nope/rows", params={"as_of": "2026-09-26T00:00:00"}
    )
    assert response.status_code == 404
    assert response.json()["title"] == "unknown_factor"
    assert response.headers["content-type"].startswith("application/problem+json")
