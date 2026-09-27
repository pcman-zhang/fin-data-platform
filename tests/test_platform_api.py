"""管理 API（TASK-3.21）单测：TestClient + 注入上下文（字典 / 内存仓储 / 桩读取器）。

真库集成见 ``tests/test_integration_api.py``（``-m integration``）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.derived.store import AlgorithmRow, InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.registry.models import (
    CodeHistoryRecord,
    EntityRecord,
    ExternalIdRecord,
    RelationTypeRecord,
)
from fin_data_platform.registry.reader import RelationView
from fin_data_platform.runtime._util import utcnow
from fin_data_platform.runtime.models import JobDef, JobIntent, JobKind, JobStatus
from fin_data_platform.runtime.repository import InMemoryMetaRepository
from fin_data_platform.storage.config import StorageConfig
from fin_data_platform.storage.schema import build_metadata

DATASET = "cn_equity.daily_bar"


class _FakeRegistry:
    """读取面桩：覆盖 entities 路由所需的全部方法。"""

    def __init__(self) -> None:
        self.lifecycle_rows: list[dict] = []
        self._entities = {
            10001: EntityRecord(
                entity_id=10001,
                entity_type="equity",
                entity_class="stock",
                market="cn",
                code="600519.SH",
                name="贵州茅台",
                currency="CNY",
                exchange="SSE",
                valid_from=date(2001, 8, 27),
                valid_to=None,
                knowledge_time=datetime(2026, 9, 1),
                version=1,
            ),
            10002: EntityRecord(
                entity_id=10002,
                entity_type="issuer",
                market="cn",
                code="91520000714308124W",
                name="贵州茅台酒股份有限公司",
                social_status="active",
                valid_from=date(2001, 1, 1),
                valid_to=None,
                knowledge_time=datetime(2026, 9, 1),
                version=1,
            ),
        }

    def search_entities(self, *, query=None, entity_type=None, market=None, limit=50, offset=0):
        items = list(self._entities.values())
        if entity_type:
            items = [item for item in items if item.entity_type == entity_type]
        if market:
            items = [item for item in items if item.market == market]
        if query:
            needle = query.lower()
            items = [
                item
                for item in items
                if needle in item.code.lower() or needle in item.name.lower()
            ]
        return items[offset : offset + limit], len(items)

    def entity(self, entity_id: int):
        return self._entities.get(entity_id)

    def entity_many(self, entity_ids, *, as_of=None):
        return {
            entity_id: record
            for entity_id in entity_ids
            if (record := self._entities.get(entity_id)) is not None
        }

    def lifecycle(self, *, knowledge_as_of=None):
        if knowledge_as_of is None:
            return list(self.lifecycle_rows)
        limit = pd.Timestamp(knowledge_as_of)
        return [
            row
            for row in self.lifecycle_rows
            if pd.Timestamp(row["knowledge_time"]) <= limit
        ]

    def entity_history(self, entity_id: int):
        record = self._entities.get(entity_id)
        return [record] if record else []

    def code_history(self, entity_id: int):
        if entity_id != 10001:
            return []
        return [
            CodeHistoryRecord(
                entity_id=10001,
                code="600519.SH",
                valid_from=date(2001, 8, 27),
                valid_to=None,
                knowledge_time=datetime(2026, 9, 1),
                version=1,
            )
        ]

    def relations(self, entity_id: int):
        if entity_id != 10001:
            return []
        return [
            RelationView(
                relation_type="issued_by",
                direction="out",
                entity_id=10001,
                related_id=10002,
                related_code="91520000714308124W",
                related_name="贵州茅台酒股份有限公司",
                valid_from=date(2001, 1, 1),
                valid_to=None,
            )
        ]

    def external_ids(self, entity_id: int):
        if entity_id != 10001:
            return []
        return [
            ExternalIdRecord(
                entity_id=10001,
                id_type="isin",
                id_value="CNE0000018R8",
                valid_from=date(2001, 8, 27),
                valid_to=None,
                knowledge_time=datetime(2026, 9, 1),
                version=1,
            )
        ]

    def relation_types(self):
        return [
            RelationTypeRecord(
                relation_type="issued_by",
                inverse_relation="issues",
                description="发行主体",
                valid_from=date(2001, 1, 1),
                valid_to=None,
                knowledge_time=datetime(2026, 9, 1),
                version=1,
            )
        ]


@pytest.fixture()
def meta() -> InMemoryMetaRepository:
    repository = InMemoryMetaRepository()
    repository.sync_defs(
        [
            JobDef(
                job_id=f"sync.{DATASET}.600519.SH",
                kind=JobKind.SYNC.value,
                dataset=DATASET,
            )
        ]
    )
    return repository


@pytest.fixture()
def algorithms() -> InMemoryAlgorithmStore:
    return InMemoryAlgorithmStore()


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
    return engine


def _seed_calendar(engine, open_days: list[date]) -> None:  # type: ignore[no-untyped-def]
    metadata, _ = build_metadata()
    table = metadata.tables["ref.trade_calendar"]
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {
                    "exchange_id": "XSHG",
                    "trade_date": day,
                    "is_open": True,
                    "pretrade_date": None,
                    "knowledge_time": datetime(2026, 9, 1),
                    "version": 1,
                }
                for day in open_days
            ],
        )


@pytest.fixture()
def registry_stub() -> _FakeRegistry:
    return _FakeRegistry()


@pytest.fixture()
def client(
    engine, meta: InMemoryMetaRepository, algorithms: InMemoryAlgorithmStore, registry_stub
) -> TestClient:
    context = ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=meta,
        algorithms=algorithms,
        registry=registry_stub,  # type: ignore[arg-type]
        specs=load_all(),
    )
    return TestClient(create_app(context, web_dist=None))


# ---------------------------------------------------------------- 数据集
def test_datasets_list_and_detail(client: TestClient) -> None:
    response = client.get("/v1/datasets", params={"domain": "cn_equity"})
    assert response.status_code == 200
    items = response.json()
    assert any(item["dataset"] == DATASET for item in items)
    detail = client.get(f"/v1/datasets/{DATASET}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["domain"] == "cn_equity"
    assert any(field["name"] == "entity_id" for field in body["fields"])
    assert body["storage"]["canonical_table"] == "cn_equity.daily_bar"


def test_dataset_404(client: TestClient) -> None:
    assert client.get("/v1/datasets/no.such_dataset").status_code == 404


# ---------------------------------------------------------------- 实体
def test_entities_search_and_detail(client: TestClient) -> None:
    response = client.get("/v1/entities", params={"query": "茅台", "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["code"].startswith("600519") or body["items"][0][
        "code"
    ].startswith("9152")

    detail = client.get("/v1/entities/10001").json()
    assert detail["code"] == "600519.SH"
    assert detail["code_history"][0]["code"] == "600519.SH"
    assert detail["relations"][0]["relation_type"] == "issued_by"
    assert detail["external_ids"][0]["id_value"] == "CNE0000018R8"
    assert detail["history"][0]["version"] == 1


def test_entities_filters_and_404(client: TestClient) -> None:
    body = client.get("/v1/entities", params={"entity_type": "issuer"}).json()
    assert body["total"] == 1 and body["items"][0]["entity_id"] == 10002
    assert client.get("/v1/entities/99999").status_code == 404
    types = client.get("/v1/entities/relation-types").json()
    assert types[0]["inverse_relation"] == "issues"


# ---------------------------------------------------------------- 任务
def test_jobs_list_filters_and_detail(
    client: TestClient, meta: InMemoryMetaRepository
) -> None:
    run = meta.create_run(
        JobIntent(
            kind=JobKind.SYNC.value,
            job_id=f"sync.{DATASET}.600519.SH",
            dataset=DATASET,
            scope="600519.SH",
            window_start=date(2026, 9, 10),
            window_end=date(2026, 9, 11),
        ),
        request_id="req-1",
    )
    assert run is not None

    body = client.get("/v1/jobs", params={"status": "queued"}).json()
    assert len(body) == 1 and body[0]["run_id"] == run.run_id
    assert body[0]["window_start"] == "2026-09-10"
    assert client.get("/v1/jobs", params={"job_id": "nope"}).json() == []

    detail = client.get(f"/v1/jobs/{run.run_id}").json()
    assert detail["request_id"] == "req-1"
    assert client.get("/v1/jobs/999999").status_code == 404
    # 非法状态过滤 → 422（契约校验）
    assert client.get("/v1/jobs", params={"status": "bogus"}).status_code == 422


def test_sync_trigger_creates_intent(
    client: TestClient, meta: InMemoryMetaRepository
) -> None:
    meta.set_watermark(
        DATASET, scope="600519.SH", watermark_time=datetime(2026, 9, 10)
    )
    response = client.post(
        "/v1/jobs/sync",
        json={"codes": ["600519.SH"], "request_id": "manual-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert len(body["submitted"]) == 1
    item = body["submitted"][0]
    # 缺省窗口：水位 +1 → 今日
    assert item["window_start"] == "2026-09-11"
    assert item["status"] == JobStatus.QUEUED.value
    run = meta.get_run(item["run_id"])
    assert run is not None and run.scope == "600519.SH"


def test_sync_trigger_skips_unregistered(client: TestClient) -> None:
    body = client.post("/v1/jobs/sync", json={"codes": ["000000.XX"]}).json()
    assert body["submitted"] == []
    assert "未注册" in body["skipped"][0]["note"]

    # 非法请求体（空代码清单）→ 422
    assert client.post("/v1/jobs/sync", json={"codes": []}).status_code == 422


def test_sync_trigger_rejects_invalid_window(client: TestClient) -> None:
    # 窗口倒置 / 未来日期 → 契约校验 422（防止水位被顶到未来导致同步停摆）
    assert (
        client.post(
            "/v1/jobs/sync",
            json={"codes": ["600519.SH"], "start": "2026-09-12", "end": "2026-09-11"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/jobs/sync",
            json={"codes": ["600519.SH"], "end": "2030-01-01"},
        ).status_code
        == 422
    )


def test_sync_trigger_request_id_idempotent(
    client: TestClient, meta: InMemoryMetaRepository
) -> None:
    meta.set_watermark(DATASET, scope="600519.SH", watermark_time=datetime(2026, 9, 10))
    first = client.post(
        "/v1/jobs/sync", json={"codes": ["600519.SH"], "request_id": "manual-9"}
    ).json()
    run_id = first["submitted"][0]["run_id"]

    # 相同 request_id（即使窗口不同）→ 返回既有运行，不重复入队
    replay = client.post(
        "/v1/jobs/sync",
        json={
            "codes": ["600519.SH"],
            "start": "2026-09-01",
            "end": "2026-09-02",
            "request_id": "manual-9",
        },
    ).json()
    assert replay["submitted"][0]["run_id"] == run_id
    assert "幂等键命中" in replay["submitted"][0]["note"]

    # 不同 request_id、同窗口 → 按窗口去重（job_key 幂等）
    duplicate = client.post(
        "/v1/jobs/sync", json={"codes": ["600519.SH"], "request_id": "manual-10"}
    ).json()
    assert duplicate["submitted"] == []
    assert "窗口重复" in duplicate["skipped"][0]["note"]


# ---------------------------------------------------------------- 系统
def test_healthz_and_openapi(client: TestClient) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert set(body["checks"]) >= {"database", "dictionary"}
    assert "ok" in body and "errors" in body

    schema = client.get("/api/openapi.json")
    assert schema.status_code == 200
    assert "/v1/jobs/sync" in schema.json()["paths"]


# ---------------------------------------------------------------- 派生算法（TASK-3.22）
def test_algorithms_empty_state(client: TestClient) -> None:
    assert client.get("/v1/algorithms").json() == []
    assert client.get("/v1/algorithms/events").json() == []
    assert client.get("/v1/algorithms/generations").json() == []


def test_algorithms_registry_events_and_generations(
    client: TestClient, algorithms: InMemoryAlgorithmStore
) -> None:
    from datetime import date as _date

    from fin_data_platform.derived.store import AlgorithmEvent, AlgorithmRow

    algorithms.upsert(
        [
            AlgorithmRow(
                algorithm_id="qfq_close_v1",
                version=1,
                owner="derived-engine",
                implementation="fin_data_platform.derived.price.qfq_close",
                dataset=DATASET,
                output="qfq_close",
                inputs=(f"{DATASET}.close", "cn_equity.adj_factor.adj_factor"),
                description="前复权收盘价",
                status="active",
                effective_from=None,
            ),
            AlgorithmRow(
                algorithm_id="legacy_close_v1",
                version=1,
                owner="derived-engine",
                implementation="fin_data_platform.derived.price.legacy",
                dataset=None,
                output=None,
                inputs=(),
                description="历史实现（已退役）",
                status="deprecated",
                effective_from=_date(2026, 9, 14),
            ),
        ]
    )
    algorithms.record_events(
        [AlgorithmEvent("legacy_close_v1", _date(2026, 9, 14), "被 qfq_close_v1 取代")]
    )
    algorithms.set_generation("mart.derived_daily_bar_qfq_close", "20260915T000000Z")

    rows = client.get("/v1/algorithms").json()
    assert [row["algorithm_id"] for row in rows] == ["legacy_close_v1", "qfq_close_v1"]
    active = next(row for row in rows if row["status"] == "active")
    assert active["dataset"] == DATASET
    assert active["inputs"] == [f"{DATASET}.close", "cn_equity.adj_factor.adj_factor"]
    deprecated = next(row for row in rows if row["status"] == "deprecated")
    assert deprecated["output"] is None and deprecated["inputs"] == []

    events = client.get("/v1/algorithms/events").json()
    assert events == [
        {
            "algorithm_id": "legacy_close_v1",
            "effective_from": "2026-09-14",
            "reason": "被 qfq_close_v1 取代",
        }
    ]

    generations = client.get("/v1/algorithms/generations").json()
    assert len(generations) == 1
    assert generations[0]["read_model"] == "mart.derived_daily_bar_qfq_close"
    assert generations[0]["generation"] == "20260915T000000Z"


def test_openapi_includes_algorithm_paths(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/v1/algorithms" in paths
    assert "/v1/algorithms/events" in paths
    assert "/v1/algorithms/generations" in paths


# ---------------------------------------------------------------- 控制面意图（TASK-3.26）
def test_sync_trigger_default_end_is_last_closed(
    engine, client: TestClient, meta: InMemoryMetaRepository
) -> None:  # type: ignore[no-untyped-def]
    today = utcnow().date()
    last_closed = today - timedelta(days=3)
    _seed_calendar(engine, [last_closed - timedelta(days=2), last_closed])
    meta.set_watermark(
        DATASET,
        scope="600519.SH",
        watermark_time=datetime.combine(last_closed - timedelta(days=1), time(0, 0)),
    )
    body = client.post("/v1/jobs/sync", json={"codes": ["600519.SH"]}).json()
    item = body["submitted"][0]
    assert item["window_end"] == last_closed.isoformat()  # 缺省终点 = 最近已收盘
    assert item["window_start"] == last_closed.isoformat()  # 水位 + 1


def test_sync_trigger_rejects_end_beyond_last_closed(engine, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    today = utcnow().date()
    last_closed = today - timedelta(days=3)
    _seed_calendar(engine, [last_closed])
    response = client.post(
        "/v1/jobs/sync",
        json={"codes": ["600519.SH"], "end": (today - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 422
    assert "最近已收盘" in response.json()["detail"]


def test_materialize_endpoint_idempotent(
    client: TestClient, meta: InMemoryMetaRepository, algorithms: InMemoryAlgorithmStore
) -> None:
    meta.sync_defs(
        [
            JobDef(
                job_id="derive.cn_equity.daily_bar.ma20",
                kind=JobKind.DERIVE.value,
                dataset=DATASET,
            )
        ]
    )
    algorithms.upsert(
        [
            AlgorithmRow(
                algorithm_id="ma20",
                version=1,
                owner="derived-engine",
                implementation="fin_data_platform.derived.factors.ma20",
                dataset=DATASET,
                output="ma20",
                inputs=("cn_equity.daily_bar.close@hfq",),
                description="test",
                status="active",
                effective_from=None,
            )
        ]
    )
    response = client.post(
        "/v1/jobs/materialize", json={"factor": "ma20", "request_id": "mat-1"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == "derive.cn_equity.daily_bar.ma20"
    assert body["dataset"] == DATASET and body["output"] == "ma20"
    assert body["version_dimension"] == "ma20@v1"
    assert body["created"] is True
    assert body["window_end"] == utcnow().date().isoformat()

    replay = client.post(
        "/v1/jobs/materialize", json={"factor": "ma20", "request_id": "mat-1"}
    ).json()
    assert replay["created"] is False and replay["run_id"] == body["run_id"]
    assert "幂等" in (replay["note"] or "")


def test_materialize_endpoint_errors(client: TestClient) -> None:
    # 因子不存在 → 404；字典存在但未注册物化任务 → 409
    assert client.post("/v1/jobs/materialize", json={"factor": "no_such"}).status_code == 404
    assert client.post("/v1/jobs/materialize", json={"factor": "adx"}).status_code == 409


def test_sync_trigger_request_id_rejects_multi_code(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs/sync",
        json={"codes": ["600519.SH", "000001.SZ"], "request_id": "multi-1"},
    )
    assert response.status_code == 422
    assert "单代码" in response.json()["detail"]


# ------------------------------------------------ 通用触发与 PIT Universe（TASK-3.35）
GLOBAL_JOB = "sync.reference.market_registry"
GLOBAL_DATASET = "cn_equity.listing_lifecycle"
GLOBAL_DATASET = "cn_equity.listing_lifecycle"


def test_job_defs_and_trigger_endpoint(
    client: TestClient, meta: InMemoryMetaRepository
) -> None:
    meta.sync_defs(
        [
            JobDef(
                job_id=GLOBAL_JOB,
                kind=JobKind.SYNC.value,
                dataset=GLOBAL_DATASET,
                priority=120,
            )
        ]
    )
    defs = {item["job_id"]: item for item in client.get("/v1/jobs/defs").json()}
    assert defs[f"sync.{DATASET}.600519.SH"]["scope"] == "600519.SH"  # 按代码：派生 scope
    assert defs[GLOBAL_JOB]["scope"] == ""  # 全局任务

    response = client.post(
        "/v1/jobs/trigger", json={"job_id": GLOBAL_JOB, "request_id": "reg-1"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True and body["status"] == "queued"
    assert body["window_start"] == body["window_end"]  # 触发日窗口

    replay = client.post(
        "/v1/jobs/trigger", json={"job_id": GLOBAL_JOB, "request_id": "reg-1"}
    ).json()
    assert replay["created"] is False and replay["run_id"] == body["run_id"]
    assert "幂等" in (replay["note"] or "")

    # 错误分支：未注册 404 / 按代码任务 409（提示 ensure）/ derive 409（提示 materialize）
    assert client.post("/v1/jobs/trigger", json={"job_id": "sync.nope"}).status_code == 404
    per_code = client.post(
        "/v1/jobs/trigger", json={"job_id": f"sync.{DATASET}.600519.SH"}
    )
    assert per_code.status_code == 409 and "ensure" in per_code.json()["detail"]
    meta.sync_defs(
        [
            JobDef(
                job_id=f"derive.{DATASET}.ma20",
                kind=JobKind.DERIVE.value,
                dataset=DATASET,
            )
        ]
    )
    derive = client.post("/v1/jobs/trigger", json={"job_id": f"derive.{DATASET}.ma20"})
    assert derive.status_code == 409 and "materialize" in derive.json()["detail"]


def test_entity_universe_endpoint(client: TestClient, registry_stub) -> None:  # type: ignore[no-untyped-def]
    registry_stub.lifecycle_rows = [
        {
            "entity_id": 10001,
            "status": "listed",
            "start_date": date(2001, 8, 27),
            "end_date": None,
            "knowledge_time": datetime(2026, 9, 1),
            "version": 1,
        },
        {
            "entity_id": 10002,
            "status": "delisted",
            "start_date": date(2010, 1, 1),
            "end_date": date(2020, 12, 31),
            "knowledge_time": datetime(2026, 9, 1),
            "version": 1,
        },
    ]
    body = client.get("/v1/entities/universe", params={"as_of": "2015-01-01"}).json()
    assert body["total"] == 1 and body["items"][0]["entity_id"] == 10001

    later = client.get("/v1/entities/universe", params={"as_of": "2021-06-01"}).json()
    assert later["total"] == 1 and later["items"][0]["entity_id"] == 10001  # 10002 已过区间

    # 严格 PIT：纠正版本（knowledge 2026-09-20）在 knowledge_as_of=09-10 时不可见
    registry_stub.lifecycle_rows.append(
        {
            "entity_id": 10001,
            "status": "delisted",
            "start_date": date(2015, 1, 1),
            "end_date": None,
            "knowledge_time": datetime(2026, 9, 20),
            "version": 2,
        }
    )
    strict = client.get(
        "/v1/entities/universe",
        params={"as_of": "2015-06-01", "knowledge_as_of": "2026-09-10T00:00:00"},
    ).json()
    assert strict["total"] == 1  # 仍按当时知识（listed）判断
    corrected = client.get(
        "/v1/entities/universe", params={"as_of": "2015-06-01"}
    ).json()
    assert corrected["total"] == 0  # 当前知识：2015-01-01 起已退市
