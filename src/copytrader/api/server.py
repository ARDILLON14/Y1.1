"""FastAPI application factory: middleware, error handling, routes, static dashboard."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from copytrader.api.auth import Authenticator
from copytrader.core.errors import AuthError, ConfigError, CopyTraderError
from copytrader.resilience.rate_limiter import TokenBucket
from copytrader.security.passwords import FieldCipher
from copytrader.security.redaction import REDACTOR

log = structlog.get_logger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@dataclass
class ApiContext:
    container: Any
    application: Any
    auth: Authenticator


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


class RateLimit(BaseHTTPMiddleware):
    def __init__(self, app: Any, per_minute: int) -> None:
        super().__init__(app)
        self.per_minute = per_minute
        self._buckets: dict[str, TokenBucket] = {}
        self._last_gc = time.monotonic()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/"):
            ip = request.client.host if request.client else "unknown"
            bucket = self._buckets.get(ip)
            if bucket is None:
                bucket = TokenBucket(self.per_minute / 60, capacity=max(10, self.per_minute / 4))
                self._buckets[ip] = bucket
            if not bucket.try_acquire():
                return JSONResponse({"detail": "demasiadas peticiones"}, status_code=429)
            if time.monotonic() - self._last_gc > 600 and len(self._buckets) > 1000:
                self._buckets.clear()
                self._last_gc = time.monotonic()
        return await call_next(request)


def create_app(container: Any, application: Any = None) -> FastAPI:
    from copytrader.api.routes import analytics, dashboard, trading, wallets
    from copytrader.api.routes import auth as auth_routes

    cfg = container.cfg
    key = container.secrets.data_encryption_key
    cipher = FieldCipher(key.get_secret_value() if key else None)
    app = FastAPI(title="copytrader", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ApiContext(container, application, Authenticator(container.db, container.get_cfg, cipher))
    app.add_middleware(RateLimit, per_minute=cfg.api.rate_limit_per_minute)
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(ConfigError)
    async def _config_error(_: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": REDACTOR.text(str(exc))}, status_code=400)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": REDACTOR.text(str(exc))}, status_code=400)

    @app.exception_handler(CopyTraderError)
    async def _domain_error(_: Request, exc: CopyTraderError) -> JSONResponse:
        return JSONResponse({"detail": REDACTOR.text(str(exc))}, status_code=409)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        log.exception("api_unhandled_error", path=request.url.path)
        return JSONResponse({"detail": "error interno"}, status_code=500)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_routes.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(wallets.router, prefix="/api")
    app.include_router(trading.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app
