import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.integrations.prometheus_client import PrometheusClient
from app.schemas.incident import TimeWindow
from app.services import incident_store
from app.services.baseline import SIGNALS, BaselineAnalyzer, evaluate, findings_from

SIG = {s.name: s for s in SIGNALS}
rng = random.Random(7)


def noisy(level, spread, n=60):
    return [level + rng.uniform(-spread, spread) for _ in range(n)]


def ev(name, baseline, current, **kw):
    return evaluate(SIG[name], "payment-service", baseline, current,
                    z_threshold=kw.get("z", 3.0), min_points=kw.get("min_points", 10))


# --- Statistics -------------------------------------------------------------------

def test_failure_ratio_jump_is_anomalous():
    r = ev("failure_ratio", noisy(0.02, 0.01), noisy(0.9, 0.05, 10))
    assert r.anomalous and r.z_score > 3
    assert "above" in r.reason


def test_normal_variation_is_not_anomalous():
    assert not ev("latency_avg", noisy(0.20, 0.05), noisy(0.21, 0.05, 10)).anomalous


def test_up_only_signals_ignore_improvements():
    assert not ev("failure_ratio", noisy(0.5, 0.02), noisy(0.0, 0.0, 10)).anomalous


def test_traffic_drop_is_detected():
    r = ev("request_rate", noisy(5.0, 0.3), noisy(0.2, 0.05, 10))
    assert r.anomalous and r.z_score < -3 and "below" in r.reason


def test_rise_from_idle_baseline_is_not_a_spike():
    r = ev("request_rate", [0.0] * 100 + noisy(0.5, 0.05, 10), noisy(0.47, 0.05, 10))
    assert not r.anomalous and "idle baseline" in r.reason
    # but a real spike over established traffic is
    assert ev("request_rate", noisy(1.0, 0.1), noisy(6.0, 0.3, 10)).anomalous


def test_ratio_queries_drop_idle_points_instead_of_zeroing_them():
    for name in ("http_error_ratio", "latency_avg", "failure_ratio"):
        q = SIG[name].query("payment-service")
        assert "clamp_min" not in q  # 0/0 must stay NaN so idle minutes are excluded


def test_flat_baseline_tiny_change_is_ignored():
    # MAD is 0 on a flat baseline; min_delta keeps noise from becoming "infinite" z
    assert not ev("failure_ratio", [0.0] * 30, [0.01] * 10).anomalous
    assert ev("failure_ratio", [0.0] * 30, [0.4] * 10).anomalous


def test_baseline_spikes_do_not_mask_or_fake_anomalies():
    base = noisy(0.2, 0.02) + [5.0, 6.0, 7.0]  # a few outliers in "normal"
    assert ev("latency_avg", base, noisy(1.5, 0.05, 10)).anomalous
    assert not ev("latency_avg", base, noisy(0.21, 0.02, 10)).anomalous


def test_insufficient_baseline_is_reported_not_guessed():
    r = ev("latency_avg", [0.2] * 5, [3.0] * 10)
    assert not r.anomalous and "insufficient data" in r.reason


def test_findings_from_stored_baseline():
    stored = {"signals": [
        {"service": "payment-service", "signal": "failure_ratio", "anomalous": True,
         "z_score": 12.0, "reason": "failure_ratio 0.9 ratio vs baseline 0.02", "current_points": 10},
        {"service": "payment-service", "signal": "latency_avg", "anomalous": False, "z_score": 0.4, "reason": "ok"},
    ]}
    (f,) = findings_from(stored)
    assert f.anomaly_type == "baseline_failure_ratio" and f.score == 1.0 and f.service == "payment-service"
    assert findings_from(None) == []


def test_windows_have_minimum_length_and_gap():
    s = Settings(_env_file=None)
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    base, cur = BaselineAnalyzer(s).windows(TimeWindow(start=end, end=end))
    assert (cur.end - cur.start).total_seconds() == s.BASELINE_MIN_WINDOW_SECONDS
    assert (cur.start - base.end).total_seconds() == s.BASELINE_GAP_SECONDS
    assert (base.end - base.start).total_seconds() == s.BASELINE_LOOKBACK_SECONDS


# --- End to end with a stubbed Prometheus -------------------------------------------

def _matrix(values):
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": [[1790000000 + i * 30, str(v)] for i, v in enumerate(values)]}
    ]}}


def fake_prometheus(incident_start):
    """Payments started failing at incident_start; everything else is normal."""

    async def query_range(self, promql, start, end, step="15s"):
        in_incident = float(start) >= incident_start.timestamp() - 1
        is_payment = 'service_name="payment-service"' in promql
        if "fail" in promql:
            return _matrix(noisy(0.85, 0.05, 10) if (in_incident and is_payment) else noisy(0.01, 0.005, 110))
        if "_sum" in promql:
            return _matrix(noisy(0.03, 0.005, 10 if in_incident else 110))
        if "5.." in promql:
            return _matrix([0.0] * (10 if in_incident else 110))
        return _matrix(noisy(2.0, 0.2, 10 if in_incident else 110))

    return query_range


def test_endpoint_stores_baseline_and_correlation_uses_it(client):
    incident_store.clear()
    iid = client.post("/api/v1/incidents", json={
        "title": "Payments failing", "severity": "WARN",
        "affected_services": ["payment-service", "order-service"],
    }).json()["incident_id"]
    start = incident_store.get(iid).time_window.start

    before = client.post(f"/api/v1/incidents/{iid}/correlate").json()["severity"]["score"]
    with patch.object(PrometheusClient, "query_range", fake_prometheus(start - timedelta(minutes=5))):
        r = client.post(f"/api/v1/incidents/{iid}/baseline")
    assert r.status_code == 200
    body = r.json()
    assert len(body["signals"]) == 2 * len(SIGNALS)
    anomalous = [(s["service"], s["signal"]) for s in body["signals"] if s["anomalous"]]
    assert anomalous == [("payment-service", "failure_ratio")]

    stored = incident_store.get(iid).metadata["baseline"]
    assert stored["anomalies"] == 1

    corr = client.post(f"/api/v1/incidents/{iid}/correlate").json()
    kinds = [a["anomaly_type"] for a in corr["anomalies"]]
    assert "baseline_failure_ratio" in kinds
    assert corr["severity"]["score"] > before


def test_endpoint_404(client):
    incident_store.clear()
    assert client.post("/api/v1/incidents/missing/baseline").status_code == 404


def test_prometheus_down_is_reported_per_signal(client):
    incident_store.clear()
    iid = client.post("/api/v1/incidents", json={"title": "x", "affected_services": ["payment-service"]}).json()["incident_id"]

    async def down(self, *a, **kw):
        raise ConnectionError("prometheus unreachable")

    with patch.object(PrometheusClient, "query_range", down):
        body = client.post(f"/api/v1/incidents/{iid}/baseline").json()
    assert body["anomalies"] == 0
    assert all("query failed" in s["reason"] for s in body["signals"])
