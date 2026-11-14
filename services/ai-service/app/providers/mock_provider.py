"""
Deterministic mock provider.

Lets the whole service run — and lets CI run the test suite — with zero
external API keys and zero network calls.
"""
import json
from typing import Optional, Type

from app.providers.base import AIProvider, SchemaT


class MockAIProvider(AIProvider):
    name = "mock"

    async def generate_text(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        return f"[mock response for prompt of {len(prompt)} chars]"

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[SchemaT],
        system_instruction: Optional[str] = None,
    ) -> SchemaT:
        """Heuristic payloads keyed off the response model name / prompt content."""
        model_name = response_model.__name__
        lowered = prompt.lower()

        if model_name == "AgentAction":
            # The mock cannot plan; end immediately (planner treats mock answers as unavailable).
            fake_payload = {
                "thought": "Mock provider cannot plan an investigation.",
                "tool": "finish",
                "final_answer": "[mock] No AI model available to plan the investigation.",
            }
        elif model_name == "RunbookResult" or "incident response runbook" in lowered:
            if "timeout" in lowered or "gateway" in lowered:
                title = "Payment gateway timeout response"
                summary = "Orders failing because payment-service cannot reach the gateway."
                conf = 0.7
            elif "latency" in lowered:
                title = "Payment latency degradation response"
                summary = "Elevated payment latency impacting order completion."
                conf = 0.65
            else:
                title = "Generic service failure response"
                summary = "Multi-service errors detected; stabilize payment path first."
                conf = 0.5

            fake_payload = {
                "title": title,
                "summary": f"[mock] {summary}",
                "steps": [
                    {
                        "order": 1,
                        "action": "Confirm blast radius — check order and payment error rates",
                        "command_or_check": "curl http://order-service:8001/api/v1/health/ready",
                        "expected_result": "Health endpoints respond; note degraded status if payment is down",
                        "rollback": None,
                    },
                    {
                        "order": 2,
                        "action": "Inspect recent payment-service logs for gateway/timeout errors",
                        "command_or_check": "docker compose logs payment-service --tail 100",
                        "expected_result": "Identify timeout or error pattern matching the incident",
                        "rollback": None,
                    },
                    {
                        "order": 3,
                        "action": "If failure injection is enabled, disable it (set FAILURE_RATE=0)",
                        "command_or_check": "Update payment-service env FAILURE_RATE=0 and restart",
                        "expected_result": "New payments succeed",
                        "rollback": "Re-enable prior FAILURE_RATE if testing requires it",
                    },
                    {
                        "order": 4,
                        "action": "Re-run a test order to verify recovery",
                        "command_or_check": "POST /api/v1/orders with a sample payload",
                        "expected_result": "status=paid",
                        "rollback": None,
                    },
                ],
                "remediation_actions": [
                    {
                        "priority": 1,
                        "action": "Restore payment-service connectivity / disable failure injection",
                        "rationale": "Primary path blocking order completion",
                        "risk_level": "low",
                        "automated": True,
                        "confidence": conf,
                    },
                    {
                        "priority": 2,
                        "action": "Add or tighten circuit breaker on order→payment calls",
                        "rationale": "Prevent cascade when payment is slow or down",
                        "risk_level": "medium",
                        "automated": False,
                        "confidence": max(0.4, conf - 0.15),
                    },
                ],
                "verification_checks": [
                    "POST /api/v1/orders returns status=paid",
                    "No new ERROR spans on payment.process in Tempo",
                    "payments_total{status=failed} rate returns to baseline",
                ],
                "escalation_guidance": "Escalate to platform on-call if payment gateway is externally down for >15 minutes.",
                "confidence": conf,
                "estimated_time_minutes": 20,
            }
        elif model_name == "RCAResult" or "root cause analysis" in lowered:
            # Infer a plausible cause from keywords in the prompt
            if "timeout" in lowered or "gateway" in lowered:
                primary = "Payment gateway timeout causing cascading order failures"
                component = "payment-service"
                confidence = 0.72
            elif "latency" in lowered:
                primary = "Elevated payment-service latency under load"
                component = "payment-service"
                confidence = 0.65
            elif "error" in lowered or "failed" in lowered:
                primary = "Downstream payment failures propagating to order-service"
                component = "payment-service"
                confidence = 0.6
            else:
                primary = "Insufficient evidence for a definitive root cause (mock provider)"
                component = "unknown"
                confidence = 0.35

            fake_payload = {
                "summary": (
                    f"[mock] Analysis suggests: {primary}. "
                    "Configure AI_PROVIDER=gemini for real LLM-based RCA."
                ),
                "primary_cause": primary,
                "hypotheses": [
                    {
                        "rank": 1,
                        "cause": primary,
                        "confidence": confidence,
                        "evidence": [
                            "Error logs and/or failed spans present in incident context",
                            "Affected services include payment and/or order paths",
                        ],
                        "affected_component": component,
                    },
                    {
                        "rank": 2,
                        "cause": "Transient network issue between order-service and payment-service",
                        "confidence": max(0.2, confidence - 0.25),
                        "evidence": ["Cross-service spans present"],
                        "affected_component": "network",
                    },
                ],
                "contributing_factors": [
                    "Lack of circuit-breaking on payment calls",
                    "No retry/backoff observed in sample path",
                ],
                "recommended_next_steps": [
                    "Inspect payment-service logs around the incident time window",
                    "Check payment gateway health and error rates",
                    "Review recent deploys to payment-service",
                ],
                "confidence": confidence,
                "evidence_gaps": [
                    "Real Prometheus quantiles for latency",
                    "Full distributed traces from Tempo",
                    "Deployment change events",
                ],
            }
        else:
            # Default: log-analysis shaped response
            if any(word in lowered for word in ("critical", "outofmemory", "panic", "fatal")):
                severity, confidence = "critical", 0.55
            elif any(word in lowered for word in ("error", "exception", "failed", "timeout")):
                severity, confidence = "high", 0.5
            elif "warn" in lowered:
                severity, confidence = "medium", 0.45
            else:
                severity, confidence = "low", 0.4

            fake_payload = {
                "severity": severity,
                "category": "unknown (mock provider)",
                "summary": "This is a placeholder summary generated by the mock AI provider.",
                "probable_root_cause": "Root cause analysis requires a real AI provider (set AI_PROVIDER=gemini).",
                "recommended_action": "Investigate manually, or configure a real AI provider for automated analysis.",
                "confidence": confidence,
            }

        return response_model.model_validate_json(json.dumps(fake_payload))
