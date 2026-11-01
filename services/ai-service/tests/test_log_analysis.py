def test_analyze_log_returns_structured_result(client):
    payload = {
        "log_content": "2026-08-22T10:00:00Z ERROR OrderService: connection timeout to PaymentService",
        "service_name": "order-service",
        "context": "Started right after a deploy",
    }
    response = client.post("/api/v1/logs/analyze", json=payload)
    assert response.status_code == 200

    body = response.json()
    assert body["provider"] == "mock"
    assert "request_id" in body

    analysis = body["analysis"]
    assert analysis["severity"] in ("low", "medium", "high", "critical")
    assert 0.0 <= analysis["confidence"] <= 1.0
    assert analysis["summary"]
    assert analysis["probable_root_cause"]
    assert analysis["recommended_action"]


def test_analyze_log_rejects_empty_content(client):
    response = client.post("/api/v1/logs/analyze", json={"log_content": ""})
    assert response.status_code == 422
