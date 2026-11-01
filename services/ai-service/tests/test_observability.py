from unittest.mock import AsyncMock, MagicMock, patch


def test_observability_health_endpoint(client):
    with patch("app.services.telemetry_query.PrometheusClient.health", new_callable=AsyncMock) as ph, \
         patch("app.services.telemetry_query.LokiClient.health", new_callable=AsyncMock) as lh, \
         patch("app.services.telemetry_query.TempoClient.health", new_callable=AsyncMock) as th:
        ph.return_value = True
        lh.return_value = False
        th.return_value = True
        r = client.get("/api/v1/observability/health")
        assert r.status_code == 200
        data = r.json()
        assert data["prometheus"] is True
        assert data["loki"] is False
        assert data["tempo"] is True


def test_metrics_query_handles_error(client):
    with patch("app.services.telemetry_query.PrometheusClient.query", new_callable=AsyncMock) as q:
        q.side_effect = Exception("connection refused")
        r = client.get("/api/v1/observability/metrics", params={"query": "up"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "error" in data
