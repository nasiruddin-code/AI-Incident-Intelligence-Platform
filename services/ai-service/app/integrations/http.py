"""Shared outbound HTTP client for observability backends.

One pooled httpx.AsyncClient per process keeps TCP connections to
Prometheus / Loki / Tempo alive across requests instead of opening a new
connection (and client) for every call. Timeouts are set per request.

The client is bound to the event loop that created it; if a different loop
asks (e.g. separate test clients), a fresh client is created for that loop.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None

_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def get_http_client() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(limits=_LIMITS)
        _client_loop = loop
    return _client


async def close_http_client() -> None:
    """Close the shared client. Called from the app lifespan on shutdown."""
    global _client, _client_loop
    client, loop = _client, _client_loop
    _client = None
    _client_loop = None
    if client is not None and not client.is_closed and loop is asyncio.get_running_loop():
        await client.aclose()
