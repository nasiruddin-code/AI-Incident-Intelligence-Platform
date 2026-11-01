"""In-process counters for AI provider calls, exported on /metrics.

Counters are per process (Prometheus scrapes each ai-service instance) and
reset on restart, so dashboards should use rate()/increase() on them.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


def _labels(**labels: Optional[str]) -> str:
    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{esc(v or "unknown")}"' for k, v in labels.items()) + "}"


class AIMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.requests: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self.tokens: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self.cost_usd: Dict[Tuple[str, str], float] = defaultdict(float)
        self.latency_sum: Dict[str, float] = defaultdict(float)
        self.latency_count: Dict[str, int] = defaultdict(int)
        self.cache_hits: Dict[str, int] = defaultdict(int)
        self.fallbacks: Dict[str, int] = defaultdict(int)

    def record_call(
        self,
        *,
        provider: str,
        operation: str,
        outcome: str,
        latency_s: float,
        model: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        model = model or "unknown"
        with self._lock:
            self.requests[(provider, operation, outcome)] += 1
            self.latency_sum[provider] += latency_s
            self.latency_count[provider] += 1
            if input_tokens:
                self.tokens[(provider, model, "input")] += input_tokens
            if output_tokens:
                self.tokens[(provider, model, "output")] += output_tokens
            if cost_usd:
                self.cost_usd[(provider, model)] += cost_usd

    def record_cache_hit(self, operation: str) -> None:
        with self._lock:
            self.cache_hits[operation] += 1

    def record_fallback(self, operation: str) -> None:
        with self._lock:
            self.fallbacks[operation] += 1

    def render(self) -> List[str]:
        with self._lock:
            lines = [
                "# HELP ai_llm_requests_total AI provider calls (each retry counts)",
                "# TYPE ai_llm_requests_total counter",
            ]
            for (provider, op, outcome), n in sorted(self.requests.items()):
                lines.append(f"ai_llm_requests_total{_labels(provider=provider, operation=op, outcome=outcome)} {n}")
            lines += [
                "# HELP ai_llm_tokens_total Tokens reported by AI providers",
                "# TYPE ai_llm_tokens_total counter",
            ]
            for (provider, model, direction), n in sorted(self.tokens.items()):
                lines.append(f"ai_llm_tokens_total{_labels(provider=provider, model=model, direction=direction)} {n}")
            lines += [
                "# HELP ai_llm_cost_usd_total Estimated spend from AI_MODEL_PRICES",
                "# TYPE ai_llm_cost_usd_total counter",
            ]
            for (provider, model), usd in sorted(self.cost_usd.items()):
                lines.append(f"ai_llm_cost_usd_total{_labels(provider=provider, model=model)} {usd:.6f}")
            lines += [
                "# HELP ai_llm_latency_seconds AI provider call latency",
                "# TYPE ai_llm_latency_seconds summary",
            ]
            for provider in sorted(self.latency_count):
                lbl = _labels(provider=provider)
                lines.append(f"ai_llm_latency_seconds_sum{lbl} {self.latency_sum[provider]:.6f}")
                lines.append(f"ai_llm_latency_seconds_count{lbl} {self.latency_count[provider]}")
            lines += [
                "# HELP ai_llm_cache_hits_total AI responses served from the response cache",
                "# TYPE ai_llm_cache_hits_total counter",
            ]
            for op, n in sorted(self.cache_hits.items()):
                lines.append(f"ai_llm_cache_hits_total{_labels(operation=op)} {n}")
            lines += [
                "# HELP ai_llm_fallbacks_total Answers served by the mock fallback (all providers failed)",
                "# TYPE ai_llm_fallbacks_total counter",
            ]
            for op, n in sorted(self.fallbacks.items()):
                lines.append(f"ai_llm_fallbacks_total{_labels(operation=op)} {n}")
            return lines


ai_metrics = AIMetrics()


def estimate_cost_usd(
    prices: Dict[str, List[float]], model: Optional[str], input_tokens: int, output_tokens: int
) -> float:
    """prices: model -> [input $/1M tokens, output $/1M tokens]."""
    if not model or model not in prices:
        return 0.0
    price_in, price_out = prices[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
