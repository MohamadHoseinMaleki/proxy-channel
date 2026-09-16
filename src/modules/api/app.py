"""FastAPI application factory for the read-only ranking API."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from structlog.contextvars import bind_contextvars, unbind_contextvars

from core.config import Settings, get_settings
from core.database import Database
from core.logger import get_logger, safe_error_message
from modules.api.schemas import (
    ErrorBody,
    HealthResponse,
    RankingResponse,
    ReadyResponse,
)
from modules.ranking.policy import DEFAULT_LIMIT, MAX_LIMIT
from modules.ranking.service import RankingService

__all__ = [
    "CACHE_CONTROL_PROXIES",
    "PROCESS_NAME",
    "REQUEST_ID_HEADER",
    "create_app",
    "resolve_request_id",
]

PROCESS_NAME = "ranking-api"
REQUEST_ID_HEADER = "x-request-id"
CACHE_CONTROL_PROXIES = "public, max-age=30"
CACHE_CONTROL_NO_STORE = "no-store"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_logger = get_logger("modules.api")


def resolve_request_id(raw: str | None) -> str:
    """Accept a bounded incoming id or mint a new one.

    Rejected values are discarded, never logged: an oversized header is a
    log-injection attempt, not a correlation id.
    """
    if raw is None:
        return uuid.uuid4().hex
    candidate = raw.strip()
    if _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def _error_body(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=ErrorBody(detail=detail).model_dump())


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Build the ranking API.

    ``database`` is injected by tests so they own engine lifetime. Production
    constructs one :class:`Database` in the lifespan and disposes it on shutdown.
    """
    cfg = settings or get_settings()
    injected = database is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Production constructs the engine on the uvicorn loop. Tests inject
        # one already bound to the pytest loop (httpx ASGITransport here does
        # not run lifespan).
        if injected:
            assert database is not None
            db = database
        else:
            db = Database.from_settings(cfg)
        app.state.db = db
        app.state.settings = cfg
        try:
            yield
        finally:
            if not injected:
                await db.dispose()

    app = FastAPI(
        title="mtproto-platform ranking API",
        version="v1",
        summary="Read-only ranking of measured MTProto proxies.",
        description=(
            "Exposes the latest Task 005/006 ranking. Does not discover, test, "
            "or score proxies. Secrets and proxy URLs are never returned."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    if injected:
        assert database is not None
        app.state.db = database
        app.state.settings = cfg

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.monotonic()
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        bind_contextvars(request_id=request_id, worker=PROCESS_NAME)
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # BaseHTTPMiddleware re-raises endpoint exceptions even when
                # FastAPI exception handlers already ran. Convert here so the
                # client always sees a generic body.
                _logger.error(
                    "http_unhandled",
                    method=request.method,
                    path=request.url.path,
                    error=safe_error_message(exc),
                    exception_type=type(exc).__name__,
                )
                response = _error_body("internal error", 500)
            else:
                _logger.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            unbind_contextvars("request_id")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # Do not echo the rejected value: query strings can be huge or crafted.
        return _error_body("invalid request", 400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error_body("not found", 404)
        if exc.status_code == 405:
            return _error_body("method not allowed", 405)
        if 400 <= exc.status_code < 500:
            return _error_body("invalid request", exc.status_code)
        return _error_body("internal error", 500)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        _logger.error(
            "http_unhandled",
            error=safe_error_message(exc),
            exception_type=type(exc).__name__,
        )
        return _error_body("internal error", 500)

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        tags=["ops"],
        summary="Liveness",
    )
    async def healthz(response: Response) -> HealthResponse:
        response.headers["Cache-Control"] = CACHE_CONTROL_NO_STORE
        return HealthResponse()

    @app.get(
        "/readyz",
        response_model=ReadyResponse,
        tags=["ops"],
        summary="Readiness",
        responses={503: {"model": ReadyResponse}},
    )
    async def readyz(request: Request) -> JSONResponse:
        db: Database = request.app.state.db
        ready = await db.is_reachable()
        status_code = 200 if ready else 503
        body = ReadyResponse(status="ready" if ready else "not_ready")
        result = JSONResponse(status_code=status_code, content=body.model_dump())
        result.headers["Cache-Control"] = CACHE_CONTROL_NO_STORE
        return result

    @app.get(
        "/v1/proxies",
        response_model=RankingResponse,
        tags=["ranking"],
        summary="Current ranked proxies",
    )
    async def list_proxies(
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> RankingResponse:
        db: Database = request.app.state.db
        page = await RankingService(db).list_top(limit=limit)
        response.headers["Cache-Control"] = CACHE_CONTROL_PROXIES
        return RankingResponse.from_page(page)

    return app
