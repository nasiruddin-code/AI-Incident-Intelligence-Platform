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

By default `AI_PROVIDER=mock`, so it runs with **no API key**. To use a real
model, set in `.env` (see `.env.example` for all options):

```
AI_PROVIDER=anthropic          # or gemini | openai | ollama
ANTHROPIC_API_KEY=your-key     # GEMINI_API_KEY / OPENAI_API_KEY for the others
AI_FALLBACK_PROVIDERS=["ollama"]   # optional: tried in order if the primary fails
```

| Provider | Default model | Notes |
|---|---|---|
| `anthropic` | `claude-opus-5-5` | Schema-validated output via `messages.parse`; server-side refusal fallback on (`ANTHROPIC_SERVER_FALLBACKS`) |
| `gemini` | `gemini-2.5-flash` | |
| `openai` | `gpt-4o-mini` | `OPENAI_BASE_URL` for any OpenAI-compatible endpoint |
| `ollama` | `llama3.1` | Local models via `OLLAMA_BASE_URL` (OpenAI-compatible API, no key) |

Each provider is retried `AI_MAX_RETRIES` times; then the next one in the chain is
tried; then the mock fallback. Responses report who answered (`provider`) and
`degraded: true` when it was the mock fallback.

## Investigation agent

`POST /api/v1/agent/investigate` runs in one of two modes (`AGENT_MODE`, or
`"mode"` in the request):

- **planner** — the model picks one tool per step (incident lookup, evidence
  search, live telemetry, PromQL/LogQL/Tempo queries, correlation, RCA,
  runbook), sees each result, and writes the final answer. Bounded by
  `max_steps`; repeat calls are de-duplicated; tools disabled via
  `run_rca` / `run_runbook` / `run_correlation` are not offered.
- **fixed** — the deterministic pipeline (resolve → evidence → correlate →
  RCA → runbook → summary).

`auto` (default) uses the planner whenever a real model is configured, and the
fixed pipeline with the mock provider or if the model is unavailable. The
response's `mode` field says which ran; each step includes the model's `thought`.

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

# List incidents (paginated, newest first; filters: status, severity)
curl "http://localhost:8000/api/v1/incidents?status=OPEN&limit=20&offset=0"

# Lightweight list without logs/metrics/traces (use this for dashboards / UIs)
curl "http://localhost:8000/api/v1/incidents/summaries?severity=ERROR"

# Pull live logs (Loki), metrics (Prometheus) and traces (Tempo) for the
# incident's services and time window into the incident. Safe to re-run.
curl -X POST http://localhost:8000/api/v1/incidents/$ID/enrich \
  -H "Content-Type: application/json" -d '{"lookback_seconds": 900}'
```

Enrichment queries Loki by `job=<service>`, Prometheus by
`service_name=<service>` (stripping the collector's `otel_` prefix and turning
`*_sum`/`*_count` into averages), and expands Tempo traces into spans. The
response reports per-backend `fetched` / `added` counts and errors.

`GET /incidents` returns at most `limit` incidents (default 50, max 500);
`total` is the number matching the filters.


## Resilience & E2E (Week 15)

- **AI fallback**: if Gemini fails, automatic retry then fall back to mock (`AI_ENABLE_FALLBACK=true`).
  Log-analysis, RCA and runbook responses report `provider` (who actually answered) and
  `degraded: true` when the fallback was used; RCA/runbook also persist `rca_degraded` /
  `runbook_degraded` in incident metadata.
- **API key auth** (optional): set `ENABLE_AUTH=true` and `API_KEY=secret`, pass `X-API-Key: secret`
- **E2E workflow**:

```bash
curl -X POST http://localhost:8000/api/v1/e2e/run \
  -H "Content-Type: application/json" \
  -d '{"title":"E2E drill","run_rca":true,"run_runbook":true}'
```
