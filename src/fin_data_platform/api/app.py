"""管理 API 应用（FastAPI）：v1 路由 + 健康检查 + SPA 静态托管。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fin_data_platform.api.deps import ApiContext, build_context
from fin_data_platform.api.routers import algorithms, datasets, entities, jobs
from fin_data_platform.api.schemas import HealthOut
from fin_data_platform.runtime.health import readiness

API_TITLE = "FinDataPlatform 管理 API"
API_VERSION = "1.0"


def _default_web_dist() -> Path | None:
    configured = os.environ.get("FDP_WEB_DIST")
    if configured:
        path = Path(configured).resolve()  # 绝对化：SPA 回退的相对路径判断
        return path if path.is_dir() else None
    candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
    return candidate.resolve() if candidate.is_dir() else None


def create_app(
    context: ApiContext | None = None, *, web_dist: Path | None = None
) -> FastAPI:
    """构建应用；``context`` / ``web_dist`` 可注入（测试与部署）。"""
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.context = context or build_context()

    app.include_router(datasets.router, prefix="/v1")
    app.include_router(entities.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(algorithms.router, prefix="/v1")

    @app.get("/healthz", response_model=HealthOut, tags=["system"], summary="健康检查")
    def healthz() -> HealthOut:
        current: ApiContext = app.state.context
        report = readiness(
            current.writer_engine, dsn=current.config.write_dsn
        )
        return HealthOut(ok=report.ok, checks=report.checks, errors=report.errors)

    dist = web_dist if web_dist is not None else _default_web_dist()
    if dist is not None:
        dist = dist.resolve()
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            """SPA 回退：静态文件存在则返回，否则返回 index.html（前端路由）。"""
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app
