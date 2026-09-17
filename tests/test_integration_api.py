"""管理 API 真库集成测试（默认跳过：``pytest -m integration``）。

验证上下文装配（读/写 DSN、字典、meta 仓储）与只读端点在生产数据库上的行为。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from fin_data_platform.api.app import create_app
from fin_data_platform.api.deps import build_context
from fin_data_platform.storage.config import StorageConfig

pytestmark = pytest.mark.integration


@pytest.fixture()
def client():
    try:
        config = StorageConfig.from_env(
            host_override=os.environ.get("FDP_DATABASE_HOST")
        )
    except ValueError as exc:
        pytest.skip(f"缺少数据库环境变量: {exc}")
    context = build_context()
    try:
        with context.writer_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 环境缺失则跳过
        pytest.skip(f"数据库不可达: {exc}")
    assert config.write_dsn  # 保持配置显式（读端缺省回退写端）
    return TestClient(create_app(context, web_dist=None))


def test_management_api_endpoints_on_real_db(client: TestClient) -> None:
    datasets = client.get("/v1/datasets").json()
    assert any(item["dataset"] == "cn_equity.daily_bar" for item in datasets)

    entities = client.get("/v1/entities", params={"limit": 5}).json()
    assert set(entities) == {"total", "limit", "offset", "items"}

    assert client.get("/v1/watermarks").status_code == 200
    assert client.get("/v1/jobs", params={"limit": 5}).status_code == 200

    health = client.get("/healthz").json()
    assert health["ok"] is True, health["errors"]

    # 触发同步：未注册代码 → 跳过（无副作用）
    response = client.post("/v1/jobs/sync", json={"codes": ["000000.XX"]})
    assert response.status_code == 202
    body = response.json()
    assert body["submitted"] == []
    assert body["skipped"] and "未注册" in body["skipped"][0]["note"]


def test_algorithm_endpoints_on_real_db(client: TestClient) -> None:
    """TASK-3.22：算法登记 / 升级台账 / 投影代次在真库上可读（空表亦返回列表）。"""
    for path in (
        "/v1/algorithms",
        "/v1/algorithms/events",
        "/v1/algorithms/generations",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert isinstance(response.json(), list), path
