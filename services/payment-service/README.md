# payment-service

Week 6–7 sample microservice. Processes payments with OpenTelemetry instrumentation.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/health` | GET | Liveness |
| `/api/v1/health/ready` | GET | Readiness |
| `/api/v1/payments` | POST | Process a payment |
| `/api/v1/payments/{id}` | GET | Get payment by ID |
| `/api/v1/payments/by-order/{order_id}` | GET | List payments for an order |

## Observability (Week 7)

- **Traces**: `payment.process` (linked to parent order span via W3C TraceContext)
- **Metrics**: `payments_total`, `payment_duration_seconds`
- Export: OTLP/HTTP → OTel Collector

## Failure injection (Week 8+ incident scenarios)

In `.env`:
```
FAILURE_RATE=0.3    # 30% of payments fail
LATENCY_MS=500      # add 500ms artificial delay
```

## Run locally

```bash
cd services/payment-service
cp .env.example .env
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8002
```
