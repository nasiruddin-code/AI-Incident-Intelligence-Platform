"""Loki HTTP API client (Week 13)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class LokiClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def query_range(
        self,
        logql: str,
        start_ns: str,
        end_ns: str,
        limit: int = 100,
        direction: str = "backward",
    ) -> Dict[str, Any]:
        params = {
            "query": logql,
            "start": start_ns,
            "end": end_ns,
            "limit": str(limit),
            "direction": direction,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/loki/api/v1/query_range", params=params)
            r.raise_for_status()
            return r.json()

    async def labels(self) -> List[str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/loki/api/v1/labels")
            r.raise_for_status()
            data = r.json()
            return data.get("data") or []

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{self.base_url}/ready")
                return r.status_code == 200
        except Exception:
            return False

    def parse_streams(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten log streams into individual log lines."""
        lines: List[Dict[str, Any]] = []
        data = result.get("data") or {}
        for stream in data.get("result") or []:
            labels = stream.get("stream") or {}
            for ts, line in stream.get("values") or []:
                lines.append({"timestamp_ns": ts, "line": line, "labels": labels})
        return lines
