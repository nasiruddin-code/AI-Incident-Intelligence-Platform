import pytest
from app.providers.base import AIProviderError
from app.providers.mock_provider import MockAIProvider
from app.providers.resilient_provider import ResilientAIProvider
from app.schemas.rca import RCAResult


class FailingProvider(MockAIProvider):
    name = "failing"

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        raise AIProviderError("simulated gemini outage")


@pytest.mark.asyncio
async def test_fallback_on_primary_failure():
    resilient = ResilientAIProvider(
        FailingProvider(),
        fallback=MockAIProvider(),
        max_retries=1,
        retry_backoff_seconds=0.01,
        enable_fallback=True,
    )
    result = await resilient.generate_structured(
        "root cause analysis timeout payment",
        RCAResult,
    )
    assert result.primary_cause
    assert "fallback" in resilient.name or result.summary


@pytest.mark.asyncio
async def test_no_fallback_raises():
    resilient = ResilientAIProvider(
        FailingProvider(),
        max_retries=0,
        retry_backoff_seconds=0.01,
        enable_fallback=False,
    )
    with pytest.raises(AIProviderError):
        await resilient.generate_structured("test", RCAResult)
