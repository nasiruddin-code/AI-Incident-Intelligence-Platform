from unittest.mock import AsyncMock, MagicMock, patch


def test_create_order_success(client):
    """Mock the outbound payment-service call."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "payment_id": "pay-abc",
        "order_id": "ignored",
        "amount": 39.98,
        "currency": "USD",
        "status": "completed",
        "payment_method": "card",
        "created_at": "2026-09-29T12:00:00Z",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post.return_value = mock_response

    with patch("app.services.order_manager.httpx.AsyncClient", return_value=mock_client):
        payload = {
            "customer_id": "cust-1",
            "items": [
                {"sku": "SKU-1", "name": "Widget", "quantity": 2, "unit_price": 19.99}
            ],
            "currency": "USD",
            "payment_method": "card",
        }
        r = client.post("/api/v1/orders", json=payload)
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "paid"
        assert data["payment_id"] == "pay-abc"
        assert data["total_amount"] == 39.98
        assert "order_id" in data


def test_order_not_found(client):
    r = client.get("/api/v1/orders/does-not-exist")
    assert r.status_code == 404
