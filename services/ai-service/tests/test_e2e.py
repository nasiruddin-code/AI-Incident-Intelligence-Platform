def test_e2e_workflow(client):
    r = client.post(
        "/api/v1/e2e/run",
        json={
            "title": "E2E test",
            "run_correlation": True,
            "run_rca": True,
            "run_runbook": True,
            "check_observability": False,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["incident_id"]
    assert data["ok"] is True
    names = [s["name"] for s in data["steps"]]
    assert "build_context" in names
    assert "correlate" in names
    assert "rca" in names
    assert "runbook" in names
