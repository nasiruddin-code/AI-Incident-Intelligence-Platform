"""Tempo HTTP API client (Week 13).

Tempo search often returns empty without an explicit time window.
We default to the last hour and support both classic tags and TraceQL.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.integrations.http import get_http_client

logger = get_logger(__name__)


class TempoClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def get_trace(self, trace_id: str) -> Dict[str, Any]:
        r = await get_http_client().get(f"{self.base_url}/api/traces/{trace_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    async def search(
        self,
        service: Optional[str] = None,
        tags: Optional[str] = None,
        min_duration: Optional[str] = None,
        max_duration: Optional[str] = None,
        limit: int = 20,
        start: Optional[int] = None,
        end: Optional[int] = None,
        lookback_seconds: int = 3600,
    ) -> Dict[str, Any]:
        """Search traces.

        Always supplies start/end (unix seconds). Tries TraceQL `q` when a
        service name is provided, which works more reliably on Tempo 2.x.
        """
        now = int(time.time())
        if end is None:
            end = now
        if start is None:
            start = now - lookback_seconds

        params: Dict[str, Any] = {
            "limit": limit,
            "start": start,
            "end": end,
        }
        if min_duration:
            params["minDuration"] = min_duration
        if max_duration:
            params["maxDuration"] = max_duration

        # Prefer TraceQL when filtering by service (Tempo 2.x)
        if service and not tags:
            params["q"] = f'{{resource.service.name="{service}"}}'
        elif tags:
            params["tags"] = tags
        elif service:
            params["tags"] = f"service.name={service}"

        client = get_http_client()
        r = await client.get(f"{self.base_url}/api/search", params=params, timeout=self.timeout)
        # If TraceQL fails (older path), retry with classic tags only
        if r.status_code >= 400 and service and "q" in params:
            logger.info("TraceQL search failed (%s), retrying with tags", r.status_code)
            params.pop("q", None)
            params["tags"] = f"service.name={service}"
            r = await client.get(f"{self.base_url}/api/search", params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()

        # Normalize response shapes
        traces = data.get("traces") or data.get("Traces") or []
        return {"traces": traces, "raw": data}

    async def search_tags(self) -> Dict[str, Any]:
        r = await get_http_client().get(f"{self.base_url}/api/search/tags", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    async def search_tag_values(self, tag: str) -> Dict[str, Any]:
        r = await get_http_client().get(f"{self.base_url}/api/search/tag/{tag}/values", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    async def health(self) -> bool:
        try:
            r = await get_http_client().get(f"{self.base_url}/ready", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False
