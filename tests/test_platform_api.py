"""管理 API（TASK-3.21）单测：TestClient + 注入上下文（字典 / 内存仓储 / 桩读取器）。

真库集成见 ``tests/test_integration_api.py``（``-m integration``）。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import ApiContext
from fin_data_platform.derived.store import InMemoryAlgorithmStore
from fin_data_platform.dictionary import load_all
from fin_data_platform.registry.models import (
    CodeHistoryRecord,
    EntityRecord,
    ExternalIdRecord,
    RelationTypeRecord,
)
from fin_data_platform.registry.reader import RelationView
from fin_data_platform.runtime.models import JobDef, JobIntent, JobKind, JobStatus
from fin_data_platform.runtime.repository import InMemoryMetaRepository
from fin_data_platform.storage.config import StorageConfig

DATASET = "cn_equity.daily_bar"


class _FakeRegistry:
    """读取面桩：覆盖 entities 路由所需的全部方法。"""

    def __init__(self) -> None:
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
def client(
    meta: InMemoryMetaRepository, algorithms: InMemoryAlgorithmStore
) -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    context = ApiContext(
        config=StorageConfig(write_dsn="sqlite://"),
        writer_engine=engine,
        read_engine=engine,
        meta=meta,
        algorithms=algorithms,
        registry=_FakeRegistry(),  # type: ignore[arg-type]
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
