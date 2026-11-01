# Production Readiness Checklist (Week 16)

## Architecture

- [x] Microservices layout (`ai-service`, `order-service`, `payment-service`)
- [x] Versioned API (`/api/v1`)
- [x] Config via environment / `.env` (12-factor)
- [x] AI provider abstraction (mock / gemini + resilient wrapper)
- [x] OpenTelemetry export to collector
- [x] Local observability stack via Docker Compose

## Functional MVP

- [x] Log analysis API
- [x] Sample order → payment flow
- [x] Incident create / context / scenarios
- [x] Correlation + anomaly + severity scoring
- [x] AI RCA + runbook generation
- [x] Investigation agent
- [x] Real Prom / Loki / Tempo query APIs
- [x] Grafana AI Insights dashboard
- [x] Prometheus alert rules
- [x] E2E workflow endpoint
- [x] Optional API-key auth
- [x] LLM retry + mock fallback

## Gaps before real production

| Area | Current | Recommended next step |
|------|---------|------------------------|
| Storage | **Postgres** when `DATABASE_URL` set; else in-memory | Redis cache optional |
| Auth | Optional static API key | OIDC / mTLS between services |
| Secrets | `.env` files | Vault / cloud secret manager |
| Scaling | Single containers | K8s + HPA |
| LLM | Mock or single Gemini key | Multi-provider + rate limits + cost controls |
| Telemetry volume | Dev retention | Longer retention, sampling |
| Alert routing | Prometheus UI only | Alertmanager → PagerDuty/Slack |
| Tests | Unit + some API tests | Full CI, load tests, contract tests |
| Multi-tenancy | None | Tenant isolation |

## Validation commands

```bash
# Unit / API tests (from each service)
cd services/ai-service && pip install -r requirements-dev.txt && pytest -q
cd services/order-service && pip install -r requirements-dev.txt && pytest -q
cd services/payment-service && pip install -r requirements-dev.txt && pytest -q

# E2E against running stack
curl -s -X POST http://localhost:8000/api/v1/e2e/run \
  -H "Content-Type: application/json" \
  -d '{"title":"validation","check_observability":true}' 
```

## Performance notes (local)

- AI calls dominate latency (especially Gemini); mock is sub-second.
- Correlation / context build are in-process and cheap.
- OTel batch export adds minimal overhead on the request path.
- For demos, prefer mock provider unless showcasing live LLM quality.

