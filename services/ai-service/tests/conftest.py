import os

# Force the mock provider for the whole test session, set BEFORE importing
# the app, so no real network/API key is ever required to run the suite.
os.environ["AI_PROVIDER"] = "mock"
os.environ["ENVIRONMENT"] = "test"
# The suite makes hundreds of requests per minute; rate-limit tests opt back in.
os.environ["RATE_LIMIT_ENABLED"] = "false"
# Environment variables beat .env, so a developer's .env (Docker hostnames for
# Postgres and the collector) can't make tests reach for real backends.
os.environ["DATABASE_URL"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    return TestClient(app)
