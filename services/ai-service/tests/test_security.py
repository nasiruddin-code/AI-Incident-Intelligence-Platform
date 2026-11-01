"""Scoped API keys, fail-closed auth, and rate limiting."""
import pytest

from app.core.auth import required_scope
from app.core.config import APIKeySpec, get_settings

READ_KEY = "read-only-key-0123456789"
ALERT_KEY = "alertmanager-key-0123456789"
AI_KEY = "ai-and-write-key-0123456789"


@pytest.fixture()
def scoped_auth(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENABLE_AUTH", True)
    monkeypatch.setattr(settings, "API_KEY", None)
    monkeypatch.setattr(settings, "API_KEYS", {
        "grafana": APIKeySpec(key=READ_KEY, scopes=["read"]),
        "alertmanager": APIKeySpec(key=ALERT_KEY, scopes=["alerts"]),
        "oncall": APIKeySpec(key=AI_KEY, scopes=["read", "write", "ai"]),
    })


@pytest.mark.parametrize("method,path,scope", [
    ("GET", "/api/v1/incidents", "read"),
    ("POST", "/api/v1/incidents", "write"),
    ("PATCH", "/api/v1/incidents/abc/status", "write"),
    ("POST", "/api/v1/incidents/abc/rca", "ai"),
    ("POST", "/api/v1/incidents/abc/runbook", "ai"),
    ("POST", "/api/v1/incidents/abc/enrich", "write"),
    ("POST", "/api/v1/logs/analyze", "ai"),
    ("POST", "/api/v1/agent/investigate", "ai"),
    ("POST", "/api/v1/alerts/alertmanager", "alerts"),
])
def test_required_scope(method, path, scope):
    assert required_scope(method, path) == scope


def test_read_key_cannot_write(scoped_auth, client):
    h = {"X-API-Key": READ_KEY}
    assert client.get("/api/v1/incidents", headers=h).status_code == 200
    r = client.post("/api/v1/incidents", json={"title": "x"}, headers=h)
    assert r.status_code == 403
    assert "'write' scope" in r.json()["detail"]


def test_alert_key_only_reaches_webhook(scoped_auth, client):
    h = {"Authorization": f"Bearer {ALERT_KEY}"}
    assert client.get("/api/v1/incidents", headers=h).status_code == 403
    r = client.post("/api/v1/alerts/alertmanager", headers=h, json={
        "groupKey": "g", "status": "resolved", "alerts": [],
    })
    assert r.status_code == 200


def test_ai_scope_required_for_model_calls(scoped_auth, client):
    body = {"log_content": "ERROR payment timeout"}
    assert client.post("/api/v1/logs/analyze", json=body, headers={"X-API-Key": READ_KEY}).status_code == 403
    assert client.post("/api/v1/logs/analyze", json=body, headers={"X-API-Key": AI_KEY}).status_code == 200


def test_query_param_key_rejected_unless_allowed(scoped_auth, client, monkeypatch):
    assert client.get(f"/api/v1/incidents?api_key={READ_KEY}").status_code == 401
    monkeypatch.setattr(get_settings(), "ALLOW_API_KEY_QUERY_PARAM", True)
    assert client.get(f"/api/v1/incidents?api_key={READ_KEY}").status_code == 200


def test_auth_enabled_without_keys_fails_closed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "ENABLE_AUTH", True)
    monkeypatch.setattr(get_settings(), "API_KEY", None)
    monkeypatch.setattr(get_settings(), "API_KEYS", {})
    assert client.get("/api/v1/incidents").status_code == 503
    assert client.get("/api/v1/health").status_code == 200


def test_short_scoped_keys_rejected_at_config_time():
    with pytest.raises(ValueError):
        APIKeySpec(key="short", scopes=["read"])


def test_cors_preflight_not_blocked_by_auth(scoped_auth, client):
    r = client.options("/api/v1/incidents", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 200


# --- Rate limiting ------------------------------------------------------------

@pytest.fixture()
def tight_limits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(settings, "AI_RATE_LIMIT_PER_MINUTE", 1)


def test_general_limit_returns_429_with_retry_after(tight_limits, client):
    codes = [client.get("/api/v1/incidents").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = client.get("/api/v1/incidents")
    assert int(r.headers["Retry-After"]) >= 1
    # Health and metrics are never limited
    assert client.get("/api/v1/health").status_code == 200


def test_ai_limit_is_stricter(tight_limits, client):
    body = {"log_content": "ERROR payment timeout"}
    assert client.post("/api/v1/logs/analyze", json=body).status_code == 200
    assert client.post("/api/v1/logs/analyze", json=body).status_code == 429
    assert client.get("/api/v1/incidents").status_code == 200  # general bucket still has room


def test_limits_are_per_api_key(tight_limits, scoped_auth, client):
    for _ in range(3):
        assert client.get("/api/v1/incidents", headers={"X-API-Key": READ_KEY}).status_code == 200
    assert client.get("/api/v1/incidents", headers={"X-API-Key": READ_KEY}).status_code == 429
    assert client.get("/api/v1/incidents", headers={"X-API-Key": AI_KEY}).status_code == 200


def test_rate_limit_headers(tight_limits, client):
    r = client.get("/api/v1/incidents")
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert r.headers["X-RateLimit-Remaining"] == "2"
