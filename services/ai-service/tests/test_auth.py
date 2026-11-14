import pytest

from app.core.config import get_settings


def test_auth_disabled_by_default(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200


@pytest.fixture()
def auth_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.setenv("API_KEY", "secret-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_missing_api_key_returns_401(auth_enabled, client):
    r = client.get("/api/v1/incidents")
    assert r.status_code == 401
    assert "X-API-Key" in r.json()["detail"]


def test_wrong_api_key_returns_401(auth_enabled, client):
    r = client.get("/api/v1/incidents", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_valid_api_key_allowed(auth_enabled, client):
    r = client.get("/api/v1/incidents", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200


def test_health_public_when_auth_enabled(auth_enabled, client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/metrics").status_code == 200
