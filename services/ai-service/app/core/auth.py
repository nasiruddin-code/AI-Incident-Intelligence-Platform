"""Simple API-key access control (Week 15)."""
from __future__ import annotations

import secrets

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
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


def _bearer_token(header: str | None) -> str | None:
    if header and header[:7].lower() == "bearer ":
        return header[7:].strip() or None
    return None


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.ENABLE_AUTH or not settings.API_KEY:
            return await call_next(request)

        path = request.url.path.rstrip("/") or "/"
        # Allow public prefixes
        if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        if path == "/metrics" or path.startswith("/metrics"):
            return await call_next(request)
        if path.endswith("/health") or path.endswith("/health/ready"):
            return await call_next(request)

        provided = (
            request.headers.get("X-API-Key")
            or _bearer_token(request.headers.get("Authorization"))  # e.g. Alertmanager webhooks
            or request.query_params.get("api_key")
        )
        if not provided or not secrets.compare_digest(provided, settings.API_KEY):
            logger.warning("Unauthorized request path=%s", path)
            # Middleware runs outside FastAPI's exception handlers, so raising
            # HTTPException here would surface as a 500; respond directly.
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or missing API key. Pass header X-API-Key."},
            )
        return await call_next(request)
