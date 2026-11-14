"""
Logging setup, shared by every service in this repo (keep the copies identical).

- LOG_FORMAT=text (default): human-readable lines for local development.
- LOG_FORMAT=json: one JSON object per line for log pipelines, including
  request_id, trace_id and span_id; uvicorn's access/error logs use it too.

RequestIDMiddleware gives every HTTP request an ID (a well-formed incoming
X-Request-ID is reused, otherwise one is generated), returns it in the
X-Request-ID response header, and tags all log records emitted while the
request is handled — including its background tasks and threadpool work.
OpenTelemetry log export (app/core/telemetry.py) attaches its own handler to
the root logger; this module only manages the stdout handler.
"""
import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | req=%(request_id)s | %(message)s"


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    def __init__(self, service: str = "") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service,
            "request_id": getattr(record, "request_id", "-"),
        }
        # Injected by the OpenTelemetry logging instrumentation when tracing is on
        trace_id = getattr(record, "otelTraceID", None)
        if trace_id and trace_id != "0":
            entry["trace_id"] = trace_id
            entry["span_id"] = getattr(record, "otelSpanID", None)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text", service: str = "") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Reuse our stdout handler across repeated calls (uvicorn --reload, tests).
    handler = next((h for h in root.handlers if getattr(h, "_app_stdout", False)), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._app_stdout = True  # type: ignore[attr-defined]
        handler.addFilter(_RequestContextFilter())
        root.addHandler(handler)

    if fmt == "json":
        handler.setFormatter(JSONFormatter(service))
        # Route uvicorn's own logs through the root handler so they are JSON too.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uv = logging.getLogger(name)
            uv.handlers = []
            uv.propagate = True
    else:
        handler.setFormatter(logging.Formatter(fmt=_TEXT_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S%z"))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class RequestIDMiddleware:
    """Pure ASGI middleware (keeps the context var visible to the whole request)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
