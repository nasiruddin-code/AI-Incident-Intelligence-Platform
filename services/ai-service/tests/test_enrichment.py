import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.loki_client import LokiClient
from app.integrations.prometheus_client import PrometheusClient
from app.integrations.tempo_client import TempoClient
from app.services import incident_store

TRACE_HEX = "0af7651916cd43dd8448eb211c80319c"
SPAN_HEX = "b7ad6b7169203331"
NOW_NS = 1_790_000_000_000_000_000

LOKI = {"data": {"result": [{
    "stream": {"job": "payment-service"},
    "values": [
        [str(NOW_NS), json.dumps({
            "body": "Payment FAILED gateway timeout",
            "severity": "ERROR",
            "traceid": TRACE_HEX,
            "attributes": {"order_id": "o-1"},
        })],
        [str(NOW_NS + 1_000_000_000), "plain warning line about latency"],
    ],
}]}}

PROM = {"status": "success", "data": {"result": [
    {"metric": {"__name__": "otel_payments_total", "service_name": "payment-service", "status": "failed"},
     "value": [1790000000, "7"]},
    {"metric": {"__name__": "otel_payment_duration_seconds_sum", "service_name": "payment-service"},
     "value": [1790000000, "10"]},
    {"metric": {"__name__": "otel_payment_duration_seconds_count", "service_name": "payment-service"},
     "value": [1790000000, "4"]},
    {"metric": {"__name__": "otel_payment_duration_seconds_bucket", "service_name": "payment-service", "le": "1"},
     "value": [1790000000, "2"]},
    {"metric": {"__name__": "target_info", "service_name": "payment-service"},
     "value": [1790000000, "1"]},
]}}

TEMPO_TRACE = {"batches": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "payment-service"}}]},
    "scopeSpans": [{"spans": [{
        "traceId": base64.b64encode(bytes.fromhex(TRACE_HEX)).decode(),
        "spanId": base64.b64encode(bytes.fromhex(SPAN_HEX)).decode(),
        "name": "payment.process",
        "startTimeUnixNano": str(NOW_NS),
        "endTimeUnixNano": str(NOW_NS + 2_000_000_000),
        "status": {"code": 2},
        "attributes": [{"key": "payment.status", "value": {"stringValue": "failed"}}],
    }]}],
}]}


@pytest.fixture()
def backends():
    with patch.object(LokiClient, "query_range", new_callable=AsyncMock, return_value=LOKI) as loki, \
         patch.object(PrometheusClient, "query", new_callable=AsyncMock, return_value=PROM) as prom, \
         patch.object(TempoClient, "search", new_callable=AsyncMock,
                      return_value={"traces": [{"traceID": TRACE_HEX}], "raw": {}}) as search, \
         patch.object(TempoClient, "get_trace", new_callable=AsyncMock, return_value=TEMPO_TRACE) as get_trace:
        yield {"loki": loki, "prom": prom, "search": search, "get_trace": get_trace}


def _incident(client):
    incident_store.clear()
    r = client.post("/api/v1/incidents", json={
        "title": "Payments failing", "severity": "WARN", "affected_services": ["payment-service"],
    })
    return r.json()["incident_id"]


def test_enrich_maps_and_merges_live_telemetry(client, backends):
    iid = _incident(client)
    r = client.post(f"/api/v1/incidents/{iid}/enrich", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["services"] == ["payment-service"]
    assert (body["logs_added"], body["metrics_added"], body["spans_added"]) == (2, 2, 1)
    assert all(s["ok"] for s in body["sources"].values())
    assert backends["loki"].call_args.args[0] == '{job="payment-service"}'
    assert backends["prom"].call_args.args[0] == '{service_name="payment-service"}'

    incident = client.get(f"/api/v1/incidents/{iid}").json()
    error_log = next(l for l in incident["logs"] if "gateway" in l["body"])
    assert error_log["severity"] == "ERROR"
    assert error_log["trace_id"] == TRACE_HEX
    assert error_log["attributes"]["order_id"] == "o-1"
    warn_log = next(l for l in incident["logs"] if "plain" in l["body"])
    assert warn_log["severity"] == "WARN"

    metrics = {m["name"]: m for m in incident["metrics"]}
    assert set(metrics) == {"payments_total", "payment_duration_seconds"}
    assert metrics["payments_total"]["type"] == "COUNTER"
    # scrape/process labels (service_name, job, ...) are not stored as attributes
    assert metrics["payments_total"]["attributes"] == {"status": "failed"}
    assert metrics["payment_duration_seconds"]["value"] == pytest.approx(2.5)

    span = incident["traces"][0]
    assert (span["trace_id"], span["span_id"], span["status"]) == (TRACE_HEX, SPAN_HEX, "ERROR")
    # Error evidence raised the incident severity via the context builder
    assert incident["severity"] == "ERROR"


def test_enrich_is_idempotent(client, backends):
    iid = _incident(client)
    client.post(f"/api/v1/incidents/{iid}/enrich")
    body = client.post(f"/api/v1/incidents/{iid}/enrich").json()
    assert (body["logs_added"], body["metrics_added"], body["spans_added"]) == (0, 0, 0)
    assert body["sources"]["loki"]["fetched"] == 2
    assert len(client.get(f"/api/v1/incidents/{iid}").json()["logs"]) == 2


def test_enrich_reports_failed_backend(client, backends):
    backends["search"].side_effect = Exception("tempo unreachable")
    iid = _incident(client)
    body = client.post(f"/api/v1/incidents/{iid}/enrich").json()
    assert body["sources"]["tempo"]["ok"] is False
    assert "tempo unreachable" in body["sources"]["tempo"]["error"]
    assert body["logs_added"] == 2 and body["spans_added"] == 0


def test_enrich_not_found(client, backends):
    incident_store.clear()
    assert client.post("/api/v1/incidents/nope/enrich").status_code == 404
