"""Evidence identity: shared by the context builder and the database layer.

Two evidence items are the same if their canonical JSON is identical, so
re-ingesting the same log/metric/span (repeat enrichment, retried requests)
never duplicates it — in memory or in Postgres (unique constraint).
"""
from __future__ import annotations

import hashlib
from typing import Iterable, List, TypeVar

from pydantic import BaseModel

ItemT = TypeVar("ItemT", bound=BaseModel)


def evidence_key(item: BaseModel) -> str:
    return hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest()


def merge_new(existing: List[ItemT], incoming: Iterable[ItemT]) -> List[ItemT]:
    """existing + items from incoming not already present (order preserved)."""
    seen = {evidence_key(i) for i in existing}
    merged = list(existing)
    for item in incoming:
        key = evidence_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged
