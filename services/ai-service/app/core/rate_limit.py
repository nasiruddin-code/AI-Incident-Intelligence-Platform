"""Per-client token-bucket rate limiting.

Two buckets per client: a general one (RATE_LIMIT_PER_MINUTE) and a stricter
one for routes that call the AI model (AI_RATE_LIMIT_PER_MINUTE), so a single
caller cannot run up model spend. Clients are identified by API key name
(set by APIKeyMiddleware) or by IP when auth is off. State is in-process.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, Tuple

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.auth import client_ip, is_ai_path, is_public
from app.core.config import get_settings

_MAX_TRACKED = 10_000
_IDLE_SECONDS = 600


class TokenBuckets:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: Dict[Tuple[str, str], Tuple[float, float]] = {}

    def take(self, key: Tuple[str, str], per_minute: int) -> Tuple[bool, int, float]:
        """Consume one token. Returns (allowed, remaining, seconds until next token)."""
        rate = per_minute / 60.0
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > _MAX_TRACKED:
                self._prune(now)
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False, 0, (1.0 - tokens) / rate
            tokens -= 1.0
            self._buckets[key] = (tokens, now)
            return True, int(tokens), 0.0

    def _prune(self, now: float) -> None:
        for key, (_, last) in list(self._buckets.items()):
            if now - last > _IDLE_SECONDS:
                del self._buckets[key]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.buckets = TokenBuckets()

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        path = request.url.path.rstrip("/") or "/"
        if not settings.RATE_LIMIT_ENABLED or request.method == "OPTIONS" or is_public(path):
            return await call_next(request)

        client = getattr(request.state, "client_id", None) or f"ip:{client_ip(request)}"
        limits = [("all", settings.RATE_LIMIT_PER_MINUTE)]
        if is_ai_path(path):
            limits.append(("ai", settings.AI_RATE_LIMIT_PER_MINUTE))

        remaining = None
        for bucket, per_minute in limits:
            allowed, left, wait = self.buckets.take((client, bucket), per_minute)
            if not allowed:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": f"Rate limit exceeded ({per_minute}/min for {bucket} requests)."},
                    headers={"Retry-After": str(max(1, math.ceil(wait)))},
                )
            if bucket == "all":
                remaining = left

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_PER_MINUTE)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
