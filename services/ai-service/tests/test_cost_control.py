import asyncio
from datetime import datetime, timezone

import pytest

from app.core.ai_metrics import AIMetrics, estimate_cost_usd
from app.providers.base import AIProvider, AIProviderError, GenerationInfo
from app.providers.mock_provider import MockAIProvider
from app.providers.resilient_provider import ResilientAIProvider
from app.providers.response_cache import ResponseCache
from app.schemas.incident import BuildContextRequest
from app.schemas.log_analysis import LogAnalysisResult
from app.schemas.telemetry import LogRecord, Resource, SeverityLevel
from app.services import incident_store
from app.services.context_builder import build_context
from app.services.rca_engine import RCAEngine

RESULT = LogAnalysisResult(
    severity="high",
    category="timeout",
    summary="s",
    probable_root_cause="r",
    recommended_action="a",
    confidence=0.7,
)


class CountingProvider(AIProvider):
    """Real-model stand-in that reports token usage."""

    name = "anthropic"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.prompts = []

    async def generate_text(self, prompt, system_instruction=None):
        return "text"

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        result, _ = await self.generate_structured_with_info(prompt, response_model, system_instruction)
        return result

    async def generate_structured_with_info(self, prompt, response_model, system_instruction=None):
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail:
            raise AIProviderError("down")
        if response_model is LogAnalysisResult:
            result = RESULT
        else:
            result = await MockAIProvider().generate_structured(prompt, response_model, system_instruction)
        info = GenerationInfo(
            provider=self.name, model="claude-opus-5-5", input_tokens=1000, output_tokens=200
        )
        return result, info


def _resilient(provider, cache=None, metrics=None, **kw):
    return ResilientAIProvider(
        provider,
        max_retries=0,
        retry_backoff_seconds=0,
        cache=cache,
        prices={"claude-opus-5-5": [4.0, 20.0]},
        metrics=metrics or AIMetrics(),
        **kw,
    )


def test_estimate_cost():
    prices = {"claude-opus-5-5": [4.0, 20.0]}
    assert estimate_cost_usd(prices, "claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(6.0)
    assert estimate_cost_usd(prices, "unknown-model", 1000, 1000) == 0.0


@pytest.mark.asyncio
async def test_usage_and_cost_are_recorded():
    metrics = AIMetrics()
    resilient = _resilient(CountingProvider(), metrics=metrics)
    result, info = await resilient.generate_structured_with_info("p", LogAnalysisResult)

    assert result == RESULT
    assert (info.input_tokens, info.output_tokens, info.model) == (1000, 200, "claude-opus-5-5")
    assert info.latency_ms >= 0
    assert metrics.requests[("anthropic", "LogAnalysisResult", "success")] == 1
    assert metrics.tokens[("anthropic", "claude-opus-5-5", "input")] == 1000
    assert metrics.cost_usd[("anthropic", "claude-opus-5-5")] == pytest.approx(0.008)

    text = "\n".join(metrics.render())
    assert 'ai_llm_tokens_total{provider="anthropic",model="claude-opus-5-5",direction="output"} 200' in text
    assert 'ai_llm_cost_usd_total{provider="anthropic",model="claude-opus-5-5"} 0.008000' in text


@pytest.mark.asyncio
async def test_failures_and_fallback_are_recorded():
    metrics = AIMetrics()
    resilient = _resilient(CountingProvider(fail=True), metrics=metrics)
    _, info = await resilient.generate_structured_with_info("p", LogAnalysisResult)
    assert info.degraded
    assert metrics.requests[("anthropic", "LogAnalysisResult", "error")] == 1
    assert metrics.requests[("mock", "LogAnalysisResult", "success")] == 1
    assert metrics.fallbacks["LogAnalysisResult"] == 1


@pytest.mark.asyncio
async def test_cache_serves_identical_requests():
    metrics = AIMetrics()
    provider = CountingProvider()
    resilient = _resilient(provider, cache=ResponseCache(60, 10), metrics=metrics)

    _, first = await resilient.generate_structured_with_info("same", LogAnalysisResult, "sys")
    result, second = await resilient.generate_structured_with_info("same", LogAnalysisResult, "sys")
    await resilient.generate_structured_with_info("different", LogAnalysisResult, "sys")
    await resilient.generate_structured_with_info("same", LogAnalysisResult, "other system")

    assert provider.calls == 3
    assert result == RESULT
    assert (first.cached, second.cached) == (False, True)
    assert second.input_tokens == 1000  # original usage kept for reference
    assert metrics.cache_hits["LogAnalysisResult"] == 1


@pytest.mark.asyncio
async def test_cache_never_stores_fallback_or_mock_answers():
    cache = ResponseCache(60, 10)
    await _resilient(CountingProvider(fail=True), cache=cache).generate_structured_with_info(
        "p", LogAnalysisResult
    )
    await _resilient(MockAIProvider(), cache=cache).generate_structured_with_info("p", LogAnalysisResult)
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_cache_expiry_and_eviction():
    cache = ResponseCache(ttl_seconds=0.05, max_entries=2)
    info = GenerationInfo(provider="anthropic")
    cache.put("a", RESULT, info)
    cache.put("b", RESULT, info)
    cache.put("c", RESULT, info)  # evicts least recently used "a"
    assert cache.get("a", LogAnalysisResult) is None
    assert cache.get("c", LogAnalysisResult) is not None
    await asyncio.sleep(0.06)
    assert cache.get("c", LogAnalysisResult) is None


@pytest.mark.asyncio
async def test_rca_prompt_ignores_its_own_previous_output():
    incident_store.clear()
    incident = build_context(BuildContextRequest(
        title="Payment timeout",
        affected_services=["payment-service"],
        notes="Paged by on-call",
        logs=[LogRecord(
            timestamp=datetime.now(timezone.utc),
            severity=SeverityLevel.ERROR,
            body="Payment FAILED gateway timeout",
            resource=Resource(service_name="payment-service"),
        )],
    ))
    provider = CountingProvider()
    engine = RCAEngine(_resilient(provider))

    first = await engine.analyze(incident.incident_id, run_correlation=False)
    await engine.analyze(incident.incident_id, run_correlation=False)

    assert provider.prompts[0] == provider.prompts[1]
    assert "Paged by on-call" in provider.prompts[1]
    assert "[RCA]" not in provider.prompts[1]
    assert first.usage.input_tokens == 1000
    stored = incident_store.get(incident.incident_id)
    assert stored.metadata["rca_prompt"] == "rca_v2"
    assert "[RCA]" in stored.notes


def test_metrics_endpoint_exposes_ai_series(client):
    body = client.get("/metrics").text
    for name in ("ai_llm_requests_total", "ai_llm_tokens_total", "ai_llm_cost_usd_total",
                 "ai_llm_cache_hits_total", "ai_llm_fallbacks_total"):
        assert f"# TYPE {name}" in body
