# ai-service

Implements Sprint 1, Weeks 1–3 of the AI Observability roadmap:

| Week | Deliverable | Where it lives |
|---|---|---|
| 1 | FastAPI foundation, config, DI, health API | `app/core/`, `app/main.py`, `app/api/v1/endpoints/health.py` |
| 2 | AI Log Analyzer (log in -> structured JSON out) | `app/services/log_analyzer.py`, `app/api/v1/endpoints/log_analysis.py` |
| 3 | Gemini + AI provider abstraction + prompt templates | `app/providers/`, `app/prompts/` |
| 4 | Telemetry & Incident Models | `app/schemas/telemetry.py`, `app/schemas/incident.py`, `app/api/v1/endpoints/telemetry.py` |
| 7 | OpenTelemetry instrumentation | `app/core/telemetry.py` — traces + metrics to OTel Collector |
| 8 | Incident scenarios + context builder | `app/services/scenario_runner.py`, `context_builder.py`, `/api/v1/incidents` |
| 9 | Correlation + anomaly detection | `app/services/correlation.py`, `POST /incidents/{id}/correlate` |
| 10 | AI-assisted RCA engine | `app/services/rca_engine.py`, `POST /incidents/{id}/rca` |
| 11 | Runbook + remediation | `app/services/runbook_engine.py`, `POST /incidents/{id}/runbook` |
| 12 | LangGraph investigation agent | `app/agent/`, `POST /agent/investigate` |
| 13 | Real Prom/Loki/Tempo queries | `app/integrations/`, `GET /observability/*` |

## Run locally

```bash
cd services/ai-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive Swagger docs.

By default `AI_PROVIDER=mock`, so it runs with **no API key**. To use real
Gemini, set in `.env`:

```
AI_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
```

## Run with Docker

```bash
docker build -t ai-service .
docker run -p 8000:8000 --env-file .env ai-service
```

Or from the repo root: `docker compose up ai-service`.

## Test

```bash
pytest
```

## Try it

```bash
curl http://127.0.0.1:8000/api/v1/health

curl -X POST http://127.0.0.1:8000/api/v1/logs/analyze \
  -H "Content-Type: application/json" \
  -d '{
        "log_content": "2026-08-22T10:00:00Z ERROR OrderService: connection timeout to PaymentService",
        "service_name": "order-service",
        "context": "Started right after a deploy"
      }'
```

## Extending this service (for later sprint weeks)

- **New AI capability (RCA, runbook generation, etc.):** add a prompt in
  `app/prompts/`, a schema in `app/schemas/`, a service in `app/services/`,
  and an endpoint in `app/api/v1/endpoints/` — then one line in
  `app/api/v1/router.py`. No existing file needs to change.
- **New AI provider (OpenAI, Bedrock, ...):** implement `AIProvider` in
  `app/providers/`, add a branch in `app/providers/factory.py`.
- **Real observability data (Prometheus/Loki/Tempo) instead of mocks:**
  these will be new provider-style clients under `app/providers/` or a new
  `app/integrations/` package, following the same interface pattern.


## Incidents (Week 8–9)

```bash
# Create an incident shell
curl -X POST http://localhost:8000/api/v1/incidents \
  -H "Content-Type: application/json" \
  -d '{"title":"Payment outage","severity":"ERROR","affected_services":["payment-service"]}'

# Build context from telemetry evidence
curl -X POST http://localhost:8000/api/v1/incidents/context \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Payment failures",
    "severity": "ERROR",
    "logs": [{
      "timestamp": "2026-09-30T10:00:00Z",
      "severity": "ERROR",
      "body": "Payment FAILED order_id=ord-1",
      "resource": {"service_name": "payment-service"}
    }]
  }'

# Run a failure scenario (set FAILURE_RATE=1.0 on payment-service first)
curl -X POST http://localhost:8000/api/v1/incidents/scenarios/run \
  -H "Content-Type: application/json" \
  -d '{"scenario":"payment_timeout","order_count":5}'
```


## Resilience & E2E (Week 15)

- **AI fallback**: if Gemini fails, automatic retry then fall back to mock (`AI_ENABLE_FALLBACK=true`)
- **API key auth** (optional): set `ENABLE_AUTH=true` and `API_KEY=secret`, pass `X-API-Key: secret`
- **E2E workflow**:

```bash
curl -X POST http://localhost:8000/api/v1/e2e/run \
  -H "Content-Type: application/json" \
  -d '{"title":"E2E drill","run_rca":true,"run_runbook":true}'
```
