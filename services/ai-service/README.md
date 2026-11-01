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

## Alerts → incidents (Alertmanager)

Prometheus alerts go to Alertmanager (`observability/alertmanager.yml`), which
posts to `POST /api/v1/alerts/alertmanager`. One alert group = one incident:

- **firing** — opens an incident (title from the alert summary, severity from
  the `severity` label, services from `service`/`service_name`/`job`, alerts as
  log evidence), then in the background pulls live telemetry, correlates, runs
  RCA and a runbook (`ALERT_AUTO_ENRICH` / `ALERT_AUTO_RCA` / `ALERT_AUTO_RUNBOOK`,
  master switch `ALERT_AUTO_INVESTIGATE`). Results land on the incident;
  `metadata.auto_investigation` records each step.
- **repeat notifications** update the same open incident; investigation re-runs
  only when new alerts join the group.
- **resolved** — marks the incident `RESOLVED`; a later firing opens a new one.

Alerts labelled `ai_investigate: "false"` (the AI service's own health alerts)
open incidents without spending AI calls. With `ENABLE_AUTH=true`, configure the
Alertmanager receiver's `http_config.authorization` (Bearer) with `API_KEY` —
the API accepts `Authorization: Bearer <API_KEY>` as well as `X-API-Key`.

Each new alert group costs up to two AI calls (RCA + runbook).

## Notifications

Incident events go to Slack and/or generic webhooks:
`incident_created`, `rca_completed`, `runbook_completed`, `incident_resolved`
(from the API, failure scenarios, and Alertmanager-driven investigations).

```
NOTIFY_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
NOTIFY_WEBHOOK_URLS=["https://example.com/hooks/incidents"]
NOTIFY_WEBHOOK_SECRET=...        # signs generic bodies: X-Signature-SHA256: sha256=<hmac>
NOTIFY_MIN_SEVERITY=WARN         # skip quieter incidents
PUBLIC_BASE_URL=https://ai.example.com   # adds incident links
```

Generic webhooks receive `{event, timestamp, incident{id,title,status,severity,
affected_services,url}, details}` with an `X-Event` header. RCA notifications
include the likely cause and confidence and flag mock-fallback answers.
Delivery runs after the response is sent; failures are logged and counted in
`ai_notifications_total{target,outcome}`, never surfaced to the caller.

## Security

**API keys with scopes** (`ENABLE_AUTH=true`). `API_KEY` is a full-access key;
`API_KEYS` adds named keys limited to scopes:

```
API_KEYS={"grafana":{"key":"<16+ chars>","scopes":["read"]},
          "alertmanager":{"key":"<16+ chars>","scopes":["alerts"]},
          "oncall":{"key":"<16+ chars>","scopes":["read","write","ai"]}}
```

| Scope | Routes |
|---|---|
| `read` | GET requests |
| `write` | other changes (create/enrich/correlate incidents, status, scenarios) |
| `ai` | routes that call the AI model: log analysis, RCA, runbook, agent, e2e |
| `alerts` | `POST /alerts/alertmanager` |

Send keys as `X-API-Key` or `Authorization: Bearer`. `?api_key=` is refused
unless `ALLOW_API_KEY_QUERY_PARAM=true` (query strings end up in logs). Health,
`/metrics` and docs stay public. With `ENABLE_AUTH=true` and no keys
configured, protected routes return 503 instead of running unauthenticated.

**Rate limits** per client (API key name, or IP when auth is off):
`RATE_LIMIT_PER_MINUTE` (default 300) for all routes and a stricter
`AI_RATE_LIMIT_PER_MINUTE` (default 30) for AI routes. Over the limit: `429`
with `Retry-After`; responses carry `X-RateLimit-Limit` / `X-RateLimit-Remaining`.
Limits are per process — with several replicas, each enforces its own.

## Logging and request IDs

`LOG_FORMAT=json` (set for all services in `docker-compose.yml`) writes one JSON
object per line — `timestamp`, `level`, `logger`, `message`, `service`,
`request_id`, `trace_id`, `span_id`, `exception` — including uvicorn's access
log. Locally the default `text` format adds `req=<id>` to each line.

Every response carries `X-Request-ID`: a well-formed incoming value is reused
(so a caller or gateway can set it), otherwise one is generated. All logs for
that request share the ID, including threadpool work and background tasks such
as Alertmanager-triggered investigations. Logs are also exported over OTLP to
Loki, where `trace_id` links them to Tempo traces.

## AI cost control

- **Usage on every response** — RCA, runbook and log-analysis responses include
  `usage` (`model`, `input_tokens`, `output_tokens`, `latency_ms`, `cached`).
- **Response cache** — identical structured requests to a real model are served
  from an in-process cache for `AI_CACHE_TTL_SECONDS` (default 1h, `0` disables).
  Fallback/mock answers are never cached. The RCA prompt leaves out the engine's
  own previous answer (`rca_*` metadata, `[RCA]` notes), so re-running RCA on
  unchanged evidence is a cache hit instead of a new, anchored answer.
- **Metrics** on `/metrics`: `ai_llm_requests_total{provider,operation,outcome}`,
  `ai_llm_tokens_total{provider,model,direction}`, `ai_llm_cost_usd_total`
  (estimated from `AI_MODEL_PRICES`), `ai_llm_latency_seconds`,
  `ai_llm_cache_hits_total`, `ai_llm_fallbacks_total`. Shown in Grafana
  (*AI cost & reliability* row) with alerts `AIProviderFallbackActive` and
  `AIProviderErrorRateHigh`.

## Evaluating prompts and models

`evals/` holds golden incidents with known root causes (gateway timeout,
latency, DB pool exhaustion, OOM crash-loop, bad deploy, and a thin-evidence
case that checks the model is *not* overconfident). Each runs through
correlation → RCA → runbook with the real prompts and is scored on: blamed
component, root-cause keywords, distractors avoided, confidence bounds, and
runbook content.

```bash
python -m evals.run_evals                       # provider from .env
python -m evals.run_evals --case payment_oom_crash --repeat 3   # variance
python -m evals.run_evals --json report.json --min-pass-rate 0.8
```

With a real provider this makes billed calls (about 2 per case per repeat;
the report prints tokens and estimated cost). Exit code is 1 below
`--min-pass-rate` and 2 if any answer came from the mock fallback. Prompt
versions (`rca_v1`, `runbook_v1`) are in the report and saved on each
incident (`rca_prompt`, `runbook_prompt`) — bump the template `name` when
changing a prompt and compare reports. CI (`tests/test_evals.py`) checks the
case files and harness with the mock provider; the keyword-matching mock
scores 3/6, which is the floor a real model should clear.

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

# Run a failure scenario (applies FAILURE_RATE/LATENCY_MS to payment-service at
# runtime, then restores them; see apply_failure_config / restore_failure_config)
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
- **API key auth** (optional): see *Security* below
- **E2E workflow**:

```bash
curl -X POST http://localhost:8000/api/v1/e2e/run \
  -H "Content-Type: application/json" \
  -d '{"title":"E2E drill","run_rca":true,"run_runbook":true}'
```
