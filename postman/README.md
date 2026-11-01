# Postman Collection — AI Incident Intelligence Platform

## Import

1. Open **Postman**
2. **Import** → choose  
   `postman/AI_Incident_Intelligence.postman_collection.json`
3. Variables are already set for local Docker:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ai_base` | `http://localhost:8000` | AI service |
| `order_base` | `http://localhost:8001` | Order service |
| `payment_base` | `http://localhost:8002` | Payment service |
| `incident_id` | auto | Filled by create/context/scenario/E2E |
| `order_id` / `payment_id` | auto | Filled by create order/payment |
| `api_key` | empty | Only if `ENABLE_AUTH=true` |
| `trace_id` | empty | From Tempo search results |

## Prerequisites

```bash
docker compose up -d --build
```

## Recommended order

| # | Folder | What to do |
|---|--------|------------|
| 0 | **Health** | Confirm AI, Order, Payment are up |
| 1 | **Sample Microservices** | Create Order (generates OTel traces) |
| 2 | **Log Analysis** | AI triage of a sample log line |
| 3 | **Telemetry Ingest** | Validate log/metric/span models |
| 4 | **Incidents** | **Build Context from Evidence** (sets `incident_id`) |
| 5 | **Correlation / RCA / Runbook** | Run in sequence on that incident |
| 6 | **Investigation Agent** | One-shot tool-based investigation |
| 7 | **Observability** | Prometheus / Loki / Tempo / `/metrics` |
| 8 | **E2E** | Full pipeline in a single call |

## What we built (mapped to folders)

```
Sample apps (Week 6–7)     → Folder 1  Order → Payment + tracing
Log Analyzer (Week 2–3)    → Folder 2  AI structured log analysis
Telemetry models (Week 4)  → Folder 3  Normalized ingest APIs
Incident context (Week 8)  → Folder 4  Context builder + scenarios
Correlation (Week 9)       → Folder 5  Links + anomalies + score
RCA (Week 10)              → Folder 5  AI root cause
Runbook (Week 11)          → Folder 5  AI remediation plan
Agent (Week 12)            → Folder 6  LangGraph-style tools
Obs queries (Week 13)      → Folder 7  Real Prom/Loki/Tempo
Dashboards metrics (14)    → Folder 7  /metrics for Grafana
Resilience + E2E (Week 15) → Folder 8  Fallback-aware full flow
```

## Core intelligence flow

```
Build Context  →  Correlate  →  RCA  →  Runbook
      ↑                           ↑
   evidence                    uses AI provider
   (logs/metrics/traces)       (mock or Gemini)
```

Or use **E2E** / **Agent Investigate** to run that chain automatically.

## Tips

- After **Build Context**, **Scenario**, or **E2E**, `incident_id` is saved by a test script.
- For Tempo: create an order first, wait a few seconds, then search traces.
- Default AI is **mock** (deterministic). Set `AI_PROVIDER=gemini` + `GEMINI_API_KEY` for live LLM.
- Scenario from the host: `order_service_url` = `http://localhost:8001` (already in the request).
- Optional auth: set collection variable `api_key` when `ENABLE_AUTH=true` on ai-service.

## Related docs

- `DEMO.md` — live demo script  
- `PRODUCTION_READINESS.md` — production gaps  
- `ROADMAP_STATUS.md` — all 16 weeks  
