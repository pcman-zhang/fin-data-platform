"""管理 API 应用（FastAPI）：v1 路由 + 健康检查 + SPA 静态托管。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fin_data_platform.access import AccessError
from fin_data_platform.api.deps import ApiContext, build_context
from fin_data_platform.api.routers import (
    algorithms,
    data,
    datasets,
    entities,
    exports,
    jobs,
    quality,
)
from fin_data_platform.api.schemas import HealthOut
from fin_data_platform.control import IntentError
from fin_data_platform.derived.errors import FactorError
from fin_data_platform.query import QueryError
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


def _schema_revision(context: ApiContext) -> str | None:
    """当前 schema 修订（SDK / 客户端兼容校验用；不可用时返回 None）。"""
    try:
        from fin_data_platform.storage.migrations import current_revision

        return current_revision(context.writer_engine)
    except Exception:
        return None


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
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.context = context or build_context()

    app.include_router(datasets.router, prefix="/v1")
    app.include_router(entities.router, prefix="/v1")
    app.include_router(data.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(algorithms.router, prefix="/v1")
    app.include_router(quality.router, prefix="/v1")
    app.include_router(exports.router, prefix="/v1")

    def _problem(
        request: Request, *, code: str, detail: str, hint: str, status: int
    ) -> JSONResponse:
        """RFC 9457 问题详情（数据面统一错误体；media type application/problem+json）。"""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        body: dict[str, object] = {
            "type": f"https://fin-data-platform/errors/{code}",
            "title": code,
            "status": status,
            "detail": detail,
            "instance": request.url.path,
            "request_id": request_id,
        }
        if hint:
            body["hint"] = hint
        return JSONResponse(
            status_code=status,
            content=body,
            media_type="application/problem+json",
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(QueryError)
    async def _query_error(request: Request, exc: QueryError) -> JSONResponse:
        return _problem(
            request, code=exc.code, detail=exc.detail, hint=exc.hint, status=exc.status
        )

    def _is_data_face(path: str) -> bool:
        return (
            path.startswith("/v1/raw/")
            or path.startswith("/v1/factors/")
            or (path.startswith("/v1/datasets/") and path.endswith("/rows"))
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """数据面参数校验失败 → RFC 9457；其余端点保持 FastAPI 默认体。"""
        if not _is_data_face(request.url.path):
            return JSONResponse(
                status_code=422,
                content={"detail": jsonable_encoder(exc.errors())},
            )
        hint = jsonable_encoder(exc.errors())[:2]
        return _problem(
            request,
            code="invalid_query",
            detail="请求参数校验失败",
            hint=json.dumps(hint, ensure_ascii=False),
            status=422,
        )

    @app.exception_handler(AccessError)
    async def _access_error(request: Request, exc: AccessError) -> JSONResponse:
        status = 404 if exc.code == "invalid_dataset" else 422
        return _problem(
            request, code=exc.code, detail=exc.detail, hint=exc.hint, status=status
        )

    @app.exception_handler(FactorError)
    async def _factor_error(request: Request, exc: FactorError) -> JSONResponse:
        status = 404 if exc.code in ("factor_not_materialized", "unknown_factor") else 422
        return _problem(
            request, code=exc.code, detail=exc.detail, hint=exc.hint, status=status
        )

    @app.exception_handler(IntentError)
    async def _intent_error(request: Request, exc: IntentError) -> JSONResponse:
        status = {
            "job_not_registered": 409,
            "not_found": 404,
            "invalid_window": 422,
            "invalid_request": 422,
            "timeout": 504,
        }.get(exc.code, 422)
        return _problem(
            request, code=exc.code, detail=exc.detail, hint=exc.hint, status=status
        )

    @app.get("/healthz", response_model=HealthOut, tags=["system"], summary="健康检查")
    def healthz() -> HealthOut:
        current: ApiContext = app.state.context
        report = readiness(
            current.writer_engine, dsn=current.config.write_dsn
        )
        return HealthOut(
            ok=report.ok,
            checks=report.checks,
            errors=report.errors,
            schema_revision=_schema_revision(current),
        )

    @app.get(
        "/v1/health", response_model=HealthOut, tags=["system"], summary="健康检查（/v1）"
    )
    def health_v1() -> HealthOut:
        return healthz()

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
