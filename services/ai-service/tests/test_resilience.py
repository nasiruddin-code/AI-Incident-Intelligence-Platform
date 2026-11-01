import pytest
from app.providers.base import AIProviderError
from app.providers.factory import get_ai_provider
from app.providers.mock_provider import MockAIProvider
from app.providers.resilient_provider import ResilientAIProvider
from app.schemas.log_analysis import LogAnalysisResult
from app.schemas.rca import RCAResult


class FailingProvider(MockAIProvider):
    name = "failing"

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        raise AIProviderError("simulated gemini outage")


class FlakyProvider(MockAIProvider):
    """Fails until `failures_left` reaches zero, then succeeds."""

    name = "flaky"

    def __init__(self, failures_left: int):
        self.failures_left = failures_left

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        if self.failures_left > 0:
            self.failures_left -= 1
            raise AIProviderError("simulated outage")
        return await super().generate_structured(prompt, response_model, system_instruction)


def _resilient(primary, **kw):
    return ResilientAIProvider(
        primary,
        fallback=MockAIProvider(),
        max_retries=kw.pop("max_retries", 1),
        retry_backoff_seconds=0.01,
        enable_fallback=kw.pop("enable_fallback", True),
    )


@pytest.mark.asyncio
async def test_fallback_on_primary_failure():
    resilient = _resilient(FailingProvider())
    result, info = await resilient.generate_structured_with_info(
        "root cause analysis timeout payment",
        RCAResult,
    )
    assert result.primary_cause
    assert info.provider == "mock"
    assert info.degraded is True


@pytest.mark.asyncio
async def test_plain_generate_structured_still_falls_back():
    resilient = _resilient(FailingProvider())
    result = await resilient.generate_structured("root cause analysis timeout", RCAResult)
    assert result.primary_cause


@pytest.mark.asyncio
async def test_fallback_does_not_relabel_shared_provider():
    """A fallback on one call must not mark later successful calls as degraded."""
    resilient = _resilient(FlakyProvider(failures_left=2), max_retries=1)

    _, first = await resilient.generate_structured_with_info("x", RCAResult)
    assert first.degraded is True

    _, second = await resilient.generate_structured_with_info("x", RCAResult)
    assert second.degraded is False
    assert second.provider == "flaky"
    assert resilient.name == "flaky"


@pytest.mark.asyncio
async def test_retry_recovers_without_degrading():
    resilient = _resilient(FlakyProvider(failures_left=1), max_retries=1)
    _, info = await resilient.generate_structured_with_info("x", LogAnalysisResult)
    assert info.provider == "flaky"
    assert info.degraded is False


@pytest.mark.asyncio
async def test_no_fallback_raises():
    resilient = _resilient(FailingProvider(), max_retries=0, enable_fallback=False)
    with pytest.raises(AIProviderError):
        await resilient.generate_structured("test", RCAResult)


def test_api_reports_degraded_on_fallback(client):
    client.app.dependency_overrides[get_ai_provider] = lambda: _resilient(FailingProvider())
    try:
        r = client.post(
            "/api/v1/logs/analyze",
            json={"log_content": "ERROR payment gateway timeout"},
        )
    finally:
        client.app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["provider"] == "mock"


def test_api_not_degraded_normally(client):
    r = client.post("/api/v1/logs/analyze", json={"log_content": "ERROR boom"})
    assert r.status_code == 200
    assert r.json()["degraded"] is False
