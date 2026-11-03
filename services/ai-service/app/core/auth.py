"""API-key access control with scopes (Week 15+).

Keys come from API_KEY (full access) and API_KEYS (named keys with scopes).
Each request needs the scope its route requires:

    alerts  POST /api/v1/alerts/*            (Alertmanager webhook)
    ai      routes that call the AI model    (log analysis, RCA, runbook, agent, e2e)
    read    other GET/HEAD requests
    write   other mutating requests

Keys are accepted as `X-API-Key`, `Authorization: Bearer <key>`, or (only if
ALLOW_API_KEY_QUERY_PARAM) `?api_key=`. With ENABLE_AUTH=true but no keys
configured, protected routes answer 503 rather than running unauthenticated.
The authenticated client name is stored on request.state.client_id for the
rate limiter.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional, Tuple

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Paths that stay open even when auth is enabled
PUBLIC_PATHS = {
    "/api/v1/health",
    "/api/v1/health/ready",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
}

AI_PATH = re.compile(r"^/api/v1/(logs/analyze|agent/|e2e/|log-monitor/run$|incidents/[^/]+/(rca|runbook)$)")


def is_public(path: str) -> bool:
    return (
        path in PUBLIC_PATHS
        or path.startswith(("/docs", "/redoc", "/metrics"))
        or path.endswith(("/health", "/health/ready"))
    )


def is_ai_path(path: str) -> bool:
    return bool(AI_PATH.match(path))


def required_scope(method: str, path: str) -> str:
    if path.startswith("/api/v1/alerts/"):
        return "alerts"
    if is_ai_path(path):
        return "ai"
    if method in ("GET", "HEAD"):
        return "read"
    return "write"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _bearer_token(header: str | None) -> str | None:
    if header and header[:7].lower() == "bearer ":
        return header[7:].strip() or None
    return None


def _identify(settings: Settings, provided: str) -> Optional[Tuple[str, set]]:
    """Return (client name, scopes) for a matching key, checking every key."""
    match: Optional[Tuple[str, set]] = None
    candidates = [("default", settings.API_KEY, {"*"})] if settings.API_KEY else []
    candidates += [(name, spec.key, set(spec.scopes)) for name, spec in settings.API_KEYS.items()]
    for name, key, scopes in candidates:
        if secrets.compare_digest(provided.encode(), key.encode()) and match is None:
            match = (name, scopes)
    return match


def _error(code: int, detail: str) -> JSONResponse:
    # Middleware runs outside FastAPI's exception handlers, so raising
    # HTTPException here would surface as a 500; respond directly.
    return JSONResponse(status_code=code, content={"detail": detail})


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        path = request.url.path.rstrip("/") or "/"
        request.state.client_id = f"ip:{client_ip(request)}"

        if not settings.ENABLE_AUTH or request.method == "OPTIONS" or is_public(path):
            return await call_next(request)

        if not settings.API_KEY and not settings.API_KEYS:
            logger.error("ENABLE_AUTH=true but no API_KEY/API_KEYS configured; refusing %s", path)
            return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication is enabled but no API keys are configured.")

        provided = request.headers.get("X-API-Key") or _bearer_token(request.headers.get("Authorization"))
        if not provided and settings.ALLOW_API_KEY_QUERY_PARAM:
            provided = request.query_params.get("api_key")
        identity = _identify(settings, provided) if provided else None
        if identity is None:
            logger.warning("Unauthorized request path=%s from %s", path, client_ip(request))
            return _error(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid or missing API key. Pass header X-API-Key or Authorization: Bearer.",
            )

        name, scopes = identity
        scope = required_scope(request.method, path)
        if "*" not in scopes and scope not in scopes:
            logger.warning("API key %r lacks scope %r for %s %s", name, scope, request.method, path)
            return _error(status.HTTP_403_FORBIDDEN, f"API key '{name}' lacks the '{scope}' scope.")

        request.state.client_id = f"key:{name}"
        return await call_next(request)
