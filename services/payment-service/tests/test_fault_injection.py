import pytest

from app.core.config import Settings, get_settings
from app.services import fault_injection

URL = "/api/v1/fault-injection"
PAYMENT = {"order_id": "ord-1", "amount": 10.0, "currency": "USD", "customer_id": "c-1", "payment_method": "card"}


@pytest.fixture(autouse=True)
def reset_state():
    fault_injection._config = None
    yield
    fault_injection._config = None


def _override(client, **settings):
    client.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **settings)


def test_runtime_change_affects_payments(client):
    assert client.get(URL).json() == {"failure_rate": 0.0, "latency_ms": 0}

    r = client.put(URL, json={"failure_rate": 1.0, "latency_ms": 0})
    assert r.status_code == 200
    assert client.post("/api/v1/payments", json=PAYMENT).json()["status"] == "failed"

    assert client.delete(URL).json() == {"failure_rate": 0.0, "latency_ms": 0}
    assert client.post("/api/v1/payments", json=PAYMENT).json()["status"] == "completed"


def test_rejects_invalid_values(client):
    assert client.put(URL, json={"failure_rate": 1.5, "latency_ms": 0}).status_code == 422
    assert client.put(URL, json={"failure_rate": 0.1, "latency_ms": -1}).status_code == 422


def test_hidden_in_production_unless_enabled(client):
    _override(client, ENVIRONMENT="production")
    assert client.get(URL).status_code == 404
    _override(client, ENVIRONMENT="production", FAULT_INJECTION_API_ENABLED=True)
    assert client.get(URL).status_code == 200


def test_token_required_when_configured(client):
    _override(client, FAULT_INJECTION_TOKEN="s3cret")
    assert client.put(URL, json={"failure_rate": 1.0}).status_code == 401
    assert client.put(URL, json={"failure_rate": 1.0}, headers={"X-Fault-Token": "wrong"}).status_code == 401
    r = client.put(URL, json={"failure_rate": 1.0}, headers={"X-Fault-Token": "s3cret"})
    assert r.status_code == 200
