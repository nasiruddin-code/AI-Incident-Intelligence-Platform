import os

# Force the mock provider for the whole test session, set BEFORE importing
# the app, so no real network/API key is ever required to run the suite.
os.environ["AI_PROVIDER"] = "mock"
os.environ["ENVIRONMENT"] = "test"
# The suite makes hundreds of requests per minute; rate-limit tests opt back in.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    return TestClient(app)
