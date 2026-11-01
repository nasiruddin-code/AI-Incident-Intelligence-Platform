"""Prometheus HTTP API client (Week 13)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def query(self, promql: str, time: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, str] = {"query": promql}
        if time:
            params["time"] = time
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/api/v1/query", params=params)
            r.raise_for_status()
            return r.json()

    async def query_range(
        self,
        promql: str,
        start: str,
        end: str,
        step: str = "15s",
    ) -> Dict[str, Any]:
        params = {"query": promql, "start": start, "end": end, "step": step}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/api/v1/query_range", params=params)
            r.raise_for_status()
            return r.json()

    async def targets(self) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/api/v1/targets")
            r.raise_for_status()
            return r.json()

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{self.base_url}/-/healthy")
                return r.status_code == 200
        except Exception:
            return False

    def parse_vector(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten instant-query vector result into simple rows."""
        rows: List[Dict[str, Any]] = []
        data = result.get("data") or {}
        for item in data.get("result") or []:
            metric = item.get("metric") or {}
            value = item.get("value")
            if value and len(value) >= 2:
                rows.append({"metric": metric, "timestamp": value[0], "value": float(value[1])})
        return rows
