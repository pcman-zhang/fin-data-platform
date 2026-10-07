"""部署工件回归（TASK-3.4）：Dockerfile / 编排 / 构建上下文约束。

只做静态约束断言（不要求本机 Docker 可用）：镜像契约、编排拓扑与凭证纪律。
真实验证（build / compose up）在任务记录中，见 docker compose 实测。
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_services_and_topology() -> None:
    compose = _compose()
    services = compose["services"]
    assert {"timescaledb", "redis", "migrate", "runtime"} <= set(services)
    # 迁移一次性完成后再启动 Runtime（避免多角色竞争）
    assert (
        services["runtime"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert services["migrate"]["restart"] == "no"
    # 拆分角色经 profile 提供
    assert services["runtime-scheduler"]["profiles"] == ["split"]
    assert services["runtime-worker"]["profiles"] == ["split"]
    # 缓存非权威：Redis 不作持久化
    redis_command = services["redis"]["command"]
    assert redis_command[:5] == ["redis-server", "--save", "", "--appendonly", "no"]
    assert "--maxmemory-policy" in redis_command
    assert "volatile-lru" in redis_command


def test_compose_credentials_not_inlined() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in ("DATABASE_PASSWORD", "POSTGRES_PASSWORD", "TUSHARE_TOKEN"):
        # 只允许环境变量引用，禁止明文
        assert f"{name}: ${{{name}" in text
    assert "change-me" not in text


def test_dockerfile_contract() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert "--check" in text
    assert "USER fdp" in text
    assert "FDP_ALEMBIC_INI" in text
    assert "FDP_ALEMBIC_SCRIPT_LOCATION" in text
    # 迁移资产显式随镜像携带（pip 不打包）
    assert "COPY alembic.ini" in text
    assert "COPY migrations" in text


def test_dockerignore_excludes_secrets_and_dev_assets() -> None:
    entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in entries
    assert ".venv" in entries
    assert "backlog" in entries
    assert "tests" in entries
    assert "*.egg-info" in entries
    # 构建必需资产不被排除
    assert "src" not in entries
    assert "migrations" not in entries


def test_export_volume_mounted_for_task_runners() -> None:
    """导出产物经共享卷：所有可能执行任务的进程（runtime / worker / service）必须挂载。"""
    services = _compose()["services"]
    for name in ("runtime", "runtime-worker", "service"):
        volumes = services[name].get("volumes") or []
        assert "exports-data:/data/exports" in volumes, f"{name} 缺少导出卷挂载"
