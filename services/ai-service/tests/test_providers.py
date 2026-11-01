import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.providers.anthropic_provider import SERVER_FALLBACK_BETA, AnthropicProvider
from app.providers.base import AIProviderError
from app.providers.factory import build_chain
from app.providers.mock_provider import MockAIProvider
from app.providers.openai_provider import OpenAICompatibleProvider
from app.providers.resilient_provider import ResilientAIProvider
from app.schemas.log_analysis import LogAnalysisResult

RESULT = LogAnalysisResult(
    severity="high",
    category="timeout",
    summary="Gateway timeouts",
    probable_root_cause="Payment gateway unreachable",
    recommended_action="Check gateway health",
    confidence=0.8,
)


# --- Anthropic --------------------------------------------------------------

def _anthropic(**kwargs):
    provider = AnthropicProvider(api_key="test-key", model="claude-opus-5-5", **kwargs)
    provider._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        parse=AsyncMock(), create=AsyncMock()
    )))
    return provider


@pytest.mark.asyncio
async def test_anthropic_structured_request_shape():
    provider = _anthropic()
    parse = provider._client.beta.messages.parse
    parse.return_value = SimpleNamespace(stop_reason="end_turn", parsed_output=RESULT)

    out = await provider.generate_structured("analyze", LogAnalysisResult, system_instruction="sre")

    assert out == RESULT
    kwargs = parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_format"] is LogAnalysisResult
    assert kwargs["system"] == "sre"
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == [SERVER_FALLBACK_BETA]


@pytest.mark.asyncio
async def test_anthropic_without_server_fallbacks():
    provider = _anthropic(server_fallbacks=False)
    parse = provider._client.beta.messages.parse
    parse.return_value = SimpleNamespace(stop_reason="end_turn", parsed_output=RESULT)
    await provider.generate_structured("analyze", LogAnalysisResult)
    assert "fallbacks" not in parse.call_args.kwargs
    assert "betas" not in parse.call_args.kwargs


@pytest.mark.asyncio
async def test_anthropic_refusal_raises_provider_error():
    provider = _anthropic()
    provider._client.beta.messages.parse.return_value = SimpleNamespace(
        stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"), parsed_output=None
    )
    with pytest.raises(AIProviderError, match="refusal"):
        await provider.generate_structured("analyze", LogAnalysisResult)


@pytest.mark.asyncio
async def test_anthropic_text_joins_text_blocks():
    provider = _anthropic()
    provider._client.beta.messages.create.return_value = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="hello "),
            SimpleNamespace(type="text", text="world"),
        ],
    )
    assert await provider.generate_text("hi") == "hello world"


# --- OpenAI-compatible (OpenAI / Ollama) -------------------------------------

def _openai(content, finish_reason="stop"):
    provider = OpenAICompatibleProvider(name="ollama", model="llama3.1", base_url="http://x/v1")
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason, message=SimpleNamespace(content=content)
    )]))
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return provider, create


@pytest.mark.asyncio
async def test_openai_compatible_structured():
    provider, create = _openai(RESULT.model_dump_json())
    out = await provider.generate_structured("analyze", LogAnalysisResult, system_instruction="sre")
    assert out == RESULT
    kwargs = create.call_args.kwargs
    assert kwargs["messages"][0] == {"role": "system", "content": "sre"}
    assert kwargs["response_format"]["json_schema"]["name"] == "LogAnalysisResult"


@pytest.mark.asyncio
async def test_openai_compatible_invalid_json_raises():
    provider, _ = _openai(json.dumps({"severity": "high"}))
    with pytest.raises(AIProviderError, match="did not match"):
        await provider.generate_structured("analyze", LogAnalysisResult)


@pytest.mark.asyncio
async def test_openai_compatible_truncation_raises():
    provider, _ = _openai("{", finish_reason="length")
    with pytest.raises(AIProviderError, match="truncated"):
        await provider.generate_structured("analyze", LogAnalysisResult)


# --- Factory + chain ----------------------------------------------------------

def test_build_chain_skips_unconfigured_and_dedupes():
    settings = Settings(
        _env_file=None,
        AI_PROVIDER="gemini",  # no key -> skipped
        AI_FALLBACK_PROVIDERS=["ollama", "openai", "ollama", "mock"],  # openai: no key
    )
    assert [p.name for p in build_chain(settings)] == ["ollama"]


def test_build_chain_defaults_to_mock():
    settings = Settings(_env_file=None, AI_PROVIDER="openai")
    assert [p.name for p in build_chain(settings)] == ["mock"]


class _Failing(MockAIProvider):
    name = "failing"

    async def generate_structured(self, prompt, response_model, system_instruction=None):
        raise AIProviderError("down")


@pytest.mark.asyncio
async def test_chain_uses_next_real_provider_before_mock():
    second = MockAIProvider()
    second.name = "second"
    resilient = ResilientAIProvider([_Failing(), second], max_retries=0, retry_backoff_seconds=0)
    _, info = await resilient.generate_structured_with_info("x", LogAnalysisResult)
    assert info.provider == "second"
    assert info.degraded is False
    assert resilient.name == "failing"


@pytest.mark.asyncio
async def test_chain_all_failing_falls_back_to_mock():
    resilient = ResilientAIProvider([_Failing(), _Failing()], max_retries=0, retry_backoff_seconds=0)
    _, info = await resilient.generate_structured_with_info("x", LogAnalysisResult)
    assert info.provider == "mock"
    assert info.degraded is True
