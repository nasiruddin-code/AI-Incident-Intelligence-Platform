from app.core.config import get_settings


def test_auth_disabled_by_default(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
