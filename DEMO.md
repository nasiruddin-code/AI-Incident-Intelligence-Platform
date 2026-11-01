# Demo Guide — AI Incident Intelligence Platform (MVP)

## Prerequisites

```bash
# From repo root
cp services/ai-service/.env.example services/ai-service/.env
cp services/order-service/.env.example services/order-service/.env
cp services/payment-service/.env.example services/payment-service/.env

docker compose up -d --build
docker compose ps
```

Verify backends:

```bash
curl -s http://localhost:9090/-/healthy    # Prometheus
curl -s http://localhost:3100/ready        # Loki
curl -s http://localhost:3200/ready        # Tempo
curl -s http://localhost:8000/api/v1/health
curl -s http://localhost:8001/api/v1/health
curl -s http://localhost:8002/api/v1/health
```

| UI | URL |
|----|-----|
| AI Service Swagger | http://localhost:8000/docs |
| Order Service | http://localhost:8001/docs |
| Payment Service | http://localhost:8002/docs |
| Grafana (admin/admin) | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

---

## Demo script (10–15 minutes)

### 1. Happy path — place an order

```bash
curl -s -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "demo-cust-1",
    "items": [{"sku":"SKU-1","name":"Widget","quantity":2,"unit_price":19.99}]
  }' | jq .
```

Expect `"status": "paid"`.

### 2. Show distributed traces

Open **Grafana → Explore → Tempo**, search service `order-service`.  
Or:

```bash
curl -s "http://localhost:8000/api/v1/observability/traces?service=order-service&lookback_seconds=3600" | jq .
```

### 3. Inject failure (optional)

```bash
# Set FAILURE_RATE=1.0 in payment-service and restart, OR use scenario API
curl -s -X POST http://localhost:8000/api/v1/incidents/scenarios/run \
  -H "Content-Type: application/json" \
  -d '{"scenario":"payment_timeout","order_count":5}' | jq .
```

### 4. Full E2E intelligence pipeline

```bash
curl -s -X POST http://localhost:8000/api/v1/e2e/run \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Demo payment gateway timeout",
    "run_correlation": true,
    "run_rca": true,
    "run_runbook": true,
    "check_observability": true
  }' | jq .
```

Note `incident_id` from the response.

### 5. Walk the same steps manually

```bash
ID=<incident_id>

curl -s -X POST http://localhost:8000/api/v1/incidents/$ID/correlate | jq .
curl -s -X POST http://localhost:8000/api/v1/incidents/$ID/rca | jq .
curl -s -X POST http://localhost:8000/api/v1/incidents/$ID/runbook | jq .
```

### 6. Investigation agent

```bash
curl -s -X POST http://localhost:8000/api/v1/agent/investigate \
  -H "Content-Type: application/json" \
  -d "{\"incident_id\":\"$ID\",\"question\":\"What caused the payment failures?\"}" | jq .
```

### 7. Dashboard

Grafana → **Dashboards → AI Observability → AI Incident Insights**  
Show orders/payments rates, open incidents, RCA/runbook confidence.

### 8. Resilience (optional)

With `AI_PROVIDER=gemini` and a bad key, RCA/runbook still return via **mock fallback**.

---

## Talking points

1. **Microservices + OTel** — order → payment with W3C trace propagation  
2. **Observability stack** — Prom / Loki / Tempo / Grafana  
3. **Incident Context** — normalized logs + metrics + traces  
4. **Correlation & severity** — rule-based links and 0–100 score  
5. **AI RCA + runbook** — provider abstraction (mock or Gemini)  
6. **Agent** — tool-calling investigation workflow  
7. **Resilience** — retries + fallback if LLM is down  
8. **Dashboard + alerts** — operator-facing view  

