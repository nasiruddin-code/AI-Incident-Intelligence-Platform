"""OpenTelemetry setup (Week 7), shared by every service in this repo.

Exports traces, metrics and logs to the OTel Collector via OTLP/HTTP
(logs: stdlib logging records at LOG_LEVEL and above, with trace context,
which the collector forwards to Loki).
Also instruments FastAPI and outbound HTTP clients (httpx).
"""
from __future__ import annotations

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.logging import get_logger

logger = get_logger(__name__)

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_logger_provider: LoggerProvider | None = None
_log_handler: LoggingHandler | None = None
_instrumented = False


def setup_telemetry(
    service_name: str,
    otlp_endpoint: str | None,
    app_version: str = "0.1.0",
    environment: str = "development",
) -> None:
    """Initialise TracerProvider + MeterProvider and wire OTLP exporters.

    Safe to call when otlp_endpoint is None — instrumentation is skipped
    so local unit tests and offline runs keep working.
    """
    global _tracer_provider, _meter_provider, _logger_provider, _log_handler

    if not otlp_endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — telemetry disabled")
        return

    # Ensure env var is set so any Resource detector / instrumentation picks it up
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES", f"service.name={service_name},service.version={app_version},deployment.environment={environment}")

    endpoint = otlp_endpoint.rstrip("/")
    # Strip any existing path so we can append the correct signal paths
    for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        if endpoint.endswith(suffix):
            endpoint = endpoint[: -len(suffix)]
            break
    traces_endpoint = f"{endpoint}/v1/traces"
    metrics_endpoint = f"{endpoint}/v1/metrics"
    logs_endpoint = f"{endpoint}/v1/logs"

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: app_version,
            "deployment.environment": environment,
            "telemetry.sdk.language": "python",
        }
    )

    # --- Traces -----------------------------------------------------------
    _tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=traces_endpoint)
    _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ----------------------------------------------------------
    metric_exporter = OTLPMetricExporter(endpoint=metrics_endpoint)
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_meter_provider)

    # --- Logs: forward stdlib logging records to the collector (-> Loki) ---
    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_endpoint))
    )
    set_logger_provider(_logger_provider)
    _log_handler = LoggingHandler(logger_provider=_logger_provider)
    logging.getLogger().addHandler(_log_handler)

    # --- Auto-instrumentation (httpx + logging; FastAPI done in instrument_app) -
    HTTPXClientInstrumentor().instrument()
    # Trace-context fields only: newer versions would also attach their own OTel
    # log handler (older ones ignore the flag), duplicating the handler above.
    LoggingInstrumentor().instrument(set_logging_format=True, enable_log_auto_instrumentation=False)

    logger.info(
        "OpenTelemetry initialised service=%s traces=%s metrics=%s logs=%s",
        service_name,
        traces_endpoint,
        metrics_endpoint,
        logs_endpoint,
    )


def instrument_app(app) -> None:
    """Instrument a FastAPI application. Call after setup_telemetry()."""
    global _instrumented
    if _tracer_provider is None or _instrumented:
        return
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=_tracer_provider,
        excluded_urls="health,health/ready,docs,openapi.json,redoc",
    )
    _instrumented = True


def get_tracer(name: str = __name__):
    return trace.get_tracer(name)


def get_meter(name: str = __name__):
    return metrics.get_meter(name)


def shutdown_telemetry() -> None:
    global _tracer_provider, _meter_provider, _logger_provider, _log_handler, _instrumented
    if _log_handler:
        logging.getLogger().removeHandler(_log_handler)
        _log_handler = None
    if _logger_provider:
        _logger_provider.shutdown()
        _logger_provider = None
    if _tracer_provider:
        _tracer_provider.shutdown()
        _tracer_provider = None
    if _meter_provider:
        _meter_provider.shutdown()
        _meter_provider = None
    _instrumented = False
