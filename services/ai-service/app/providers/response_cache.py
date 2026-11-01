"""TTL + LRU cache for structured AI responses.

Keyed on (provider chain, response model, system instruction, prompt), so
any change in evidence or prompt text is a miss. Values are stored as JSON
and re-validated on read, so callers never share a mutable result object.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Optional, Sequence, Tuple, Type

from app.providers.base import GenerationInfo, SchemaT


class ResponseCache:
    def __init__(self, ttl_seconds: float, max_entries: int):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._data: "OrderedDict[str, Tuple[float, str, GenerationInfo]]" = OrderedDict()

    @staticmethod
    def make_key(
        chain: Sequence[str], model_name: str, system_instruction: Optional[str], prompt: str
    ) -> str:
        raw = json.dumps([list(chain), model_name, system_instruction or "", prompt])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, response_model: Type[SchemaT]) -> Optional[Tuple[SchemaT, GenerationInfo]]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, payload, info = entry
            if expires_at < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
        return response_model.model_validate_json(payload), info

    def put(self, key: str, result: SchemaT, info: GenerationInfo) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, result.model_dump_json(), info)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
