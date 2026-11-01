"""Reproducible failure scenarios against sample microservices (Week 8).

Each scenario fires N orders against order-service (which calls payment-service)
and builds an IncidentContext from the observed outcomes + synthetic telemetry
that mirrors what would appear in Loki/Tempo/Prometheus.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from starlette.concurrency import run_in_threadpool
from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.incident import (
    IncidentContext,
    ScenarioRunRequest,
    ScenarioRunResult,
    ScenarioType,
)
from app.schemas.telemetry import (
    LogRecord,
    MetricPoint,
    MetricType,
    Resource,
    SeverityLevel,
    Span,
    SpanStatus,
)
from app.services.context_builder import build_from_scenario_results

logger = get_logger(__name__)

# Scenario → (failure_rate, latency_ms, title)
SCENARIO_CONFIG: Dict[ScenarioType, Dict[str, Any]] = {
    ScenarioType.PAYMENT_TIMEOUT: {
        "failure_rate": 1.0,
        "latency_ms": 0,
        "title": "Payment gateway timeout — all payments failing",
        "description": "Simulates complete payment-service failure (FAILURE_RATE=1.0).",
    },
    ScenarioType.PAYMENT_HIGH_LATENCY: {
        "failure_rate": 0.0,
        "latency_ms": 2000,
        "title": "Payment service high latency",
        "description": "Simulates slow payment processing (2s artificial delay).",
    },
    ScenarioType.PAYMENT_CASCADE_FAILURE: {
        "failure_rate": 0.6,
        "latency_ms": 800,
        "title": "Cascading payment failures under load",
        "description": "Mixed failures (60%) + elevated latency — cascade pattern.",
    },
}


def _order_payload(idx: int) -> dict:
    return {
        "customer_id": f"scenario-cust-{idx}",
        "items": [
            {
                "sku": f"SKU-SCENARIO-{idx}",
                "name": "Scenario Item",
                "quantity": 1,
                "unit_price": 9.99,
            }
        ],
        "currency": "USD",
        "payment_method": "card",
    }


async def _fault_injection(
    payment_service_url: str, method: str, payload: Optional[dict] = None
) -> Optional[dict]:
    """Call payment-service's /fault-injection API. Returns the config, or None on any failure."""
    url = f"{payment_service_url.rstrip('/')}/api/v1/fault-injection"
    token = get_settings().PAYMENT_FAULT_INJECTION_TOKEN
    headers = {"X-Fault-Token": token} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(method, url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.warning("Fault injection %s %s returned %s", method, url, resp.status_code)
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except httpx.HTTPError as exc:
        logger.warning("Fault injection %s %s failed: %s", method, url, exc)
        return None


async def _fire_orders(
    order_service_url: str,
    count: int,
    concurrency: int = 5,
) -> Tuple[List[dict], List[dict]]:
    """POST N orders, at most `concurrency` in flight; return (successes, failures)."""
    base = order_service_url.rstrip("/")
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(client: httpx.AsyncClient, i: int) -> Tuple[bool, dict]:
        async with limit:
            try:
                resp = await client.post(f"{base}/api/v1/orders", json=_order_payload(i))
            except httpx.HTTPError as exc:
                logger.warning("Order request failed: %s", exc)
                return False, {"error": str(exc)}
        data = resp.json() if resp.status_code < 500 else {"error": resp.text}
        if resp.status_code == 201 and data.get("status") == "paid":
            return True, data
        return False, data if isinstance(data, dict) else {"error": str(data)}

    async with httpx.AsyncClient(timeout=30.0) as client:
        outcomes = await asyncio.gather(*(one(client, i) for i in range(count)))

    successes = [data for ok, data in outcomes if ok]
    failures = [data for ok, data in outcomes if not ok]
    return successes, failures


def _synthetic_telemetry(
    scenario: ScenarioType,
    successes: List[dict],
    failures: List[dict],
    cfg: dict,
) -> Tuple[List[LogRecord], List[MetricPoint], List[Span]]:
    """Build realistic telemetry evidence from scenario outcomes."""
    now = datetime.now(timezone.utc)
    order_res = Resource(service_name="order-service", service_version="0.1.0", environment="development")
    pay_res = Resource(service_name="payment-service", service_version="0.1.0", environment="development")

    logs: List[LogRecord] = []
    metrics: List[MetricPoint] = []
    traces: List[Span] = []

    # Summary metrics
    metrics.append(
        MetricPoint(
            name="orders_total",
            description="Orders created during scenario",
            unit="1",
            type=MetricType.COUNTER,
            value=float(len(successes) + len(failures)),
            timestamp=now,
            attributes={"status": "all", "scenario": scenario.value},
            resource=order_res,
        )
    )
    metrics.append(
        MetricPoint(
            name="orders_total",
            description="Failed orders during scenario",
            unit="1",
            type=MetricType.COUNTER,
            value=float(len(failures)),
            timestamp=now,
            attributes={"status": "payment_failed", "scenario": scenario.value},
            resource=order_res,
        )
    )
    metrics.append(
        MetricPoint(
            name="payments_total",
            description="Payment attempts during scenario",
            unit="1",
            type=MetricType.COUNTER,
            value=float(len(successes) + len(failures)),
            timestamp=now,
            attributes={
                "status": "failed" if failures else "completed",
                "scenario": scenario.value,
            },
            resource=pay_res,
        )
    )
    if cfg.get("latency_ms", 0) > 0:
        metrics.append(
            MetricPoint(
                name="payment_duration_seconds",
                description="Elevated payment latency",
                unit="s",
                type=MetricType.HISTOGRAM,
                value=cfg["latency_ms"] / 1000.0,
                timestamp=now,
                attributes={"status": "completed", "scenario": scenario.value},
                resource=pay_res,
            )
        )

    # Per-failure logs + error spans
    for i, fail in enumerate(failures):
        trace_id = uuid.uuid4().hex
        order_span_id = uuid.uuid4().hex[:16]
        pay_span_id = uuid.uuid4().hex[:16]
        reason = fail.get("failure_reason") or fail.get("error") or "payment failed"

        logs.append(
            LogRecord(
                timestamp=now,
                severity=SeverityLevel.ERROR,
                body=f"Order PAYMENT_FAILED order_id={fail.get('order_id', 'unknown')} reason={reason}",
                attributes={"scenario": scenario.value, "order_id": str(fail.get("order_id", ""))},
                resource=order_res,
                trace_id=trace_id,
                span_id=order_span_id,
            )
        )
        logs.append(
            LogRecord(
                timestamp=now,
                severity=SeverityLevel.ERROR,
                body=f"Payment FAILED (injected) order_id={fail.get('order_id', 'unknown')} reason={reason}",
                attributes={"scenario": scenario.value},
                resource=pay_res,
                trace_id=trace_id,
                span_id=pay_span_id,
            )
        )
        traces.append(
            Span(
                trace_id=trace_id,
                span_id=order_span_id,
                parent_span_id=None,
                name="order.create",
                start_time=now,
                end_time=now,
                status=SpanStatus.ERROR,
                attributes={"order.status": "payment_failed", "scenario": scenario.value},
                resource=order_res,
            )
        )
        traces.append(
            Span(
                trace_id=trace_id,
                span_id=pay_span_id,
                parent_span_id=order_span_id,
                name="payment.process",
                start_time=now,
                end_time=now,
                status=SpanStatus.ERROR,
                attributes={"payment.status": "failed", "scenario": scenario.value},
                resource=pay_res,
            )
        )

    # Success spans (for cascade / latency scenarios)
    for ok in successes[:3]:  # cap to keep context small
        trace_id = uuid.uuid4().hex
        order_span_id = uuid.uuid4().hex[:16]
        traces.append(
            Span(
                trace_id=trace_id,
                span_id=order_span_id,
                parent_span_id=None,
                name="order.create",
                start_time=now,
                end_time=now,
                status=SpanStatus.OK,
                attributes={"order.status": "paid", "scenario": scenario.value},
                resource=order_res,
            )
        )

    if not failures and scenario == ScenarioType.PAYMENT_HIGH_LATENCY:
        logs.append(
            LogRecord(
                timestamp=now,
                severity=SeverityLevel.WARN,
                body=f"Elevated payment latency detected: {cfg['latency_ms']}ms (scenario={scenario.value})",
                attributes={"scenario": scenario.value, "latency_ms": cfg["latency_ms"]},
                resource=pay_res,
            )
        )

    return logs, metrics, traces


async def run_scenario(req: ScenarioRunRequest) -> ScenarioRunResult:
    """Execute a failure scenario and return a populated IncidentContext."""
    if req.scenario == ScenarioType.MANUAL:
        raise ValueError("MANUAL scenario cannot be auto-run; use POST /incidents/context instead")

    cfg = SCENARIO_CONFIG[req.scenario]
    logger.info(
        "Running scenario=%s orders=%d order_service=%s",
        req.scenario.value,
        req.order_count,
        req.order_service_url,
    )

    target = {"failure_rate": cfg["failure_rate"], "latency_ms": cfg["latency_ms"]}
    previous: Optional[dict] = None
    applied = restored = False
    if req.apply_failure_config:
        previous = await _fault_injection(req.payment_service_url, "GET")
        applied = await _fault_injection(req.payment_service_url, "PUT", target) is not None
    try:
        successes, failures = await _fire_orders(
            req.order_service_url, req.order_count, req.concurrency
        )
    finally:
        if applied and req.restore_failure_config and previous is not None:
            restored = await _fault_injection(req.payment_service_url, "PUT", previous) is not None

    logs, metrics, traces = _synthetic_telemetry(req.scenario, successes, failures, cfg)

    config_note = (
        f"Applied FAILURE_RATE={cfg['failure_rate']}, LATENCY_MS={cfg['latency_ms']} to payment-service"
        + (" and restored the previous settings afterwards." if restored else ".")
        if applied
        else (
            "Did not change payment-service settings "
            + ("(apply_failure_config=false)" if not req.apply_failure_config
               else "(fault-injection API unavailable)")
            + f"; expected FAILURE_RATE={cfg['failure_rate']}, LATENCY_MS={cfg['latency_ms']}."
        )
    )
    notes = (
        f"Scenario '{req.scenario.value}' executed: "
        f"{len(successes)} paid, {len(failures)} failed out of {req.order_count}. "
        f"{config_note} {cfg['description']}"
    )

    incident: IncidentContext = await run_in_threadpool(
        build_from_scenario_results,
        scenario=req.scenario,
        title=cfg["title"],
        affected_services=["order-service", "payment-service"],
        logs=logs,
        metrics=metrics,
        traces=traces,
        notes=notes,
        metadata={
            "orders_attempted": req.order_count,
            "orders_paid": len(successes),
            "orders_failed": len(failures),
            "expected_failure_rate": cfg["failure_rate"],
            "expected_latency_ms": cfg["latency_ms"],
            "failure_config_applied": applied,
            "failure_config_restored": restored,
        },
    )

    return ScenarioRunResult(
        scenario=req.scenario,
        incident_id=incident.incident_id,
        orders_attempted=req.order_count,
        orders_paid=len(successes),
        orders_failed=len(failures),
        message=notes,
        incident=incident,
    )
