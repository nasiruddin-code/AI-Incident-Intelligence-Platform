"""Progress events from a running investigation (for streaming to clients).

Agent code calls emit(event, data) at each milestone; it is a no-op unless a
listener was installed with `emitting(fn)` (the streaming endpoint does). A
context variable carries the listener, so it reaches LangGraph node tasks
without threading a callback through every function.

Events: mode {mode, reason?} · planning {step, max_steps} · step {ToolCallRecord}
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterator, Optional

Listener = Callable[[str, Dict[str, Any]], None]

_listener: ContextVar[Optional[Listener]] = ContextVar("agent_event_listener", default=None)


def emit(event: str, data: Dict[str, Any]) -> None:
    listener = _listener.get()
    if listener is not None:
        listener(event, data)


@contextmanager
def emitting(listener: Listener) -> Iterator[None]:
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)
