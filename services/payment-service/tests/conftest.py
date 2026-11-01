import os

# Environment variables beat .env: keep tests off the real collector.
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c
