from app.services import incident_store


def test_prometheus_metrics_endpoint(client):
    incident_store.clear()
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "ai_incidents_total" in body
    assert "ai_incidents_open" in body
