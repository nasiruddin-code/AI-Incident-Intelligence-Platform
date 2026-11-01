# AI Observability Platform

Monorepo for the 16-week AI Observability sprint. Structured as independent
microservices under `services/`, each a standalone deployable FastAPI app,
sharing common conventions so new services and capabilities slot in without
restructuring anything already built.

```
ai-observability-platform/
├── docker-compose.yml          # orchestrates all services + (later) the observability stack
├── observability/              # Week 5: Prometheus, Loki, Tempo, Grafana, OTel Collector configs
├── services/
│   ├── ai-service/             # Weeks 1-4: FastAPI, log analyzer, AI providers, telemetry models
│   ├── order-service/          # Week 6: sample Order microservice
│   └── payment-service/        # Week 6: sample Payment microservice
├── observability/              # Week 5: Prometheus, Loki, Tempo, Grafana, OTel Collector
└── (future) agent-service/
```

## Why this shape

- **Microservices, not a monolith.** `services/order-service` and
  `services/payment-service` (Week 6), and later an `agent-service` for the
  LangGraph investigation agent (Week 12+), are added as new sibling
  folders under `services/`, each independently runnable and dockerized —
  `ai-service` never has to know they exist.
- **`app/api/v1/`** is versioned from day one, so a breaking API change
  later becomes `app/api/v2/` instead of a rewrite.
- **`app/providers/`** is an abstraction over *any* external AI backend,
  not just Gemini — required for Week 3, and directly reusable for the
  "multi-provider expansion" stretch goal.
- **`app/prompts/`** keeps every prompt as a named, testable template
  instead of an inline string, which the RCA and runbook-generation
  features (Weeks 10–11) will reuse.
- **`docker-compose.yml`** already lists (commented) the observability
  stack (Prometheus/Loki/Tempo/Grafana/OTel Collector, Week 5) and the
  sample microservices (Week 6), so the target architecture is visible
  even before those pieces are built.

## Roadmap -> code mapping

| Weeks | Theme | Where it will live |
|---|---|---|
| 1–3 | AI Foundation (this delivery) | `services/ai-service` |
| 4–5 | Telemetry contracts + observability stack | `app/schemas/telemetry.py` + `observability/` (Prometheus, Loki, Tempo, Grafana, OTel Collector) |
| 6–7 | Sample microservices + OTel instrumentation | `services/order-service`, `services/payment-service` (+ ai-service) — traces, metrics, propagation |
| 8–11 | Incident intelligence (context, correlation, RCA, runbooks) | **Complete** — scenarios, context, correlation, RCA, runbooks |
| 12–13 | LangGraph agent + real telemetry queries | **Done** — `/agent/investigate` + `/observability/*` (Prom/Loki/Tempo) |
| 14–16 | Dashboards, alerting, resilience, production readiness | **Complete** — Grafana AI Insights, alerts, fallback, auth, E2E, demo docs |

## Getting started

### AI Service only (Weeks 1–4)

```bash
cd services/ai-service
cp .env.example .env
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

### Full local stack (Weeks 1–6)

```bash
# From repository root – create env files once
cp services/ai-service/.env.example services/ai-service/.env
cp services/order-service/.env.example services/order-service/.env
cp services/payment-service/.env.example services/payment-service/.env

docker compose up -d --build

# Verify observability
curl http://localhost:9090/-/healthy   # Prometheus
curl http://localhost:3100/ready       # Loki
curl http://localhost:3200/ready       # Tempo

# Verify microservices
curl http://localhost:8000/api/v1/health   # ai-service
curl http://localhost:8001/api/v1/health   # order-service
curl http://localhost:8002/api/v1/health   # payment-service

# Create a sample order (order → payment)
curl -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"cust-42","items":[{"sku":"SKU-001","name":"Widget","quantity":2,"unit_price":19.99}]}'
```

| Service          | URL / Port              |
|------------------|-------------------------|
| AI Service       | http://localhost:8000/docs |
| Order Service    | http://localhost:8001/docs |
| Payment Service  | http://localhost:8002/docs |
| Grafana          | http://localhost:3000 (admin/admin) |
| Prometheus       | http://localhost:9090 |
| Alertmanager     | http://localhost:9093 (routes alerts to ai-service → incidents) |
| Loki             | http://localhost:3100 |
| Tempo            | http://localhost:3200 |

See individual service READMEs and `observability/README.md` for details.


## Documentation

| Doc | Purpose |
|-----|---------|
| [DEMO.md](DEMO.md) | Step-by-step demo script |
| [postman/](postman/) | Postman collection + usage guide |
| [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) | Checklist + production gaps |
| [ROADMAP_STATUS.md](ROADMAP_STATUS.md) | Full 16-week status |
| [observability/README.md](observability/README.md) | Stack runbook |

## Validate MVP

```bash
docker compose up -d --build
bash scripts/validate_mvp.sh
```

**MVP status: complete (Weeks 1–16).**


## Persistence

Incidents are stored in **PostgreSQL** when `DATABASE_URL` is set (Compose default).

```bash
# Included in docker compose up
# postgres:// ai:ai@postgres:5432/ai_incidents

# Without DB (tests / minimal local): leave DATABASE_URL empty → in-memory
```
