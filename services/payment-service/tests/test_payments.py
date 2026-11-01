def test_create_payment(client):
    payload = {
        "order_id": "ord-123",
        "amount": 49.99,
        "currency": "USD",
        "customer_id": "cust-1",
        "payment_method": "card",
    }
    r = client.post("/api/v1/payments", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["order_id"] == "ord-123"
    assert data["amount"] == 49.99
    assert data["status"] in ("completed", "failed")
    assert "payment_id" in data

    # fetch it back
    r2 = client.get(f"/api/v1/payments/{data['payment_id']}")
    assert r2.status_code == 200
    assert r2.json()["payment_id"] == data["payment_id"]


def test_payment_not_found(client):
    r = client.get("/api/v1/payments/does-not-exist")
    assert r.status_code == 404
