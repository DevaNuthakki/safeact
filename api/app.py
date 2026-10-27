"""FastAPI application factory.

The API holds no authority of its own. It authenticates nothing in
Python that the database does not re-check: every product call goes
through a protected SQL function as one restricted login role (added
with the identity migration). This module wires settings, logging,
error mapping, security headers and the health endpoints.

    .venv/bin/python -m uvicorn --factory api.app:demo_app
"""

import logging
import time
import uuid
import threading
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import actions, auth, errors, events, review
from api import logging as api_logging
from observability import metrics
from api.settings import Settings, demo_settings
from storage.connection_pool import ConnectionPool


log = logging.getLogger("safeact.api")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def create_app(settings: Settings, *, connect=None) -> FastAPI:
    """Build the app; ``connect`` opens one psycopg connection (tests)."""

    pooled = connect is None
    if connect is None:
        def connect():
            return psycopg.connect(**settings.connect_kwargs())
    pool_lock = threading.Lock()

    def pooled_connection():
        with pool_lock:
            if app.state.connection_pool is None:
                app.state.connection_pool = ConnectionPool(settings.connect_kwargs())
            owner = app.state.connection_pool
        return owner.connection()

    @asynccontextmanager
    async def lifespan(_app):
        # Embedded/TestClient consumers can share one app concurrently. Each
        # lifespan borrows the app; only the final owner shuts its pool down.
        with pool_lock:
            app.state.database_lifespans += 1
        try:
            yield
        finally:
            owner = None
            with pool_lock:
                app.state.database_lifespans -= 1
                if app.state.database_lifespans == 0:
                    owner = app.state.connection_pool
                    app.state.connection_pool = None
            if owner is not None:
                owner.close()

    app = FastAPI(
        title="SafeAct API",
        version="0.1.0",
        # The interactive docs pull scripts from a CDN; the schema stays
        # available at /openapi.json for the typed UI client.
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.connect = connect
    app.state.connection_scope = pooled_connection if pooled else connect
    app.state.connection_pool = None
    app.state.database_lifespans = 0
    errors.install(app)
    app.include_router(auth.router)
    app.include_router(actions.router)
    app.include_router(review.router)
    app.include_router(events.router)
    app.state.stream_limiter = events.StreamLimiter()

    @app.middleware("http")
    async def refuse_cross_site_writes(request: Request, call_next):
        # Browsers label every request; a write from another site is
        # refused before any handler runs. The CSRF token is still
        # verified in PostgreSQL for every authenticated write.
        if request.method not in SAFE_METHODS:
            fetch_site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            own = f"{request.url.scheme}://{request.url.netloc}"
            if fetch_site in ("cross-site", "same-site") or (
                origin is not None
                and origin != own
                and origin not in settings.allowed_origins
            ):
                return JSONResponse(
                    errors.error_body(
                        "cross_site_refused",
                        "Cross-site requests are not allowed.",
                    ),
                    status_code=403,
                )
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers["X-Request-Id"] = request.state.request_id
        elapsed = time.perf_counter() - started
        # The route template ("/actions/{operation_id}"), never the raw
        # path: metric labels must not carry identifiers.
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        metrics.HTTP_REQUESTS.labels(
            request.method, route, str(response.status_code)).inc()
        metrics.HTTP_SECONDS.labels(route).observe(elapsed)
        # Path only: query strings and headers are never logged.
        log.info("request", extra={
            "request_id": request.state.request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        return response

    @app.get("/healthz", tags=["health"])
    def healthz():
        """The process is up. Says nothing about the database."""

        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def readyz():
        """The API can reach PostgreSQL as its own role."""

        try:
            with app.state.connection_scope() as connection:
                ok = connection.execute("SELECT 1").fetchone() == (1,)
        except psycopg.Error as error:
            log.warning("not_ready", extra={"error_type": type(error).__name__})
            ok = False
        if not ok:
            return JSONResponse(
                errors.error_body(
                    "unavailable", "The service is temporarily unavailable."
                ),
                status_code=503,
            )
        return {"status": "ready"}

    return app


def demo_app() -> FastAPI:
    """Uvicorn factory for the local ``safeact_demo`` environment."""

    api_logging.configure()
    metrics.serve()
    return create_app(demo_settings())
