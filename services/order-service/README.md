# order-service

Week 6–7 sample microservice. Creates orders, calls **payment-service**, and emits OpenTelemetry traces/metrics.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/health` | GET | Liveness |
| `/api/v1/health/ready` | GET | Readiness (also pings payment-service) |
| `/api/v1/orders` | POST | Create order → calls payment-service |
| `/api/v1/orders` | GET | List all orders |
| `/api/v1/orders/{id}` | GET | Get order by ID |

## Observability (Week 7)

- **Traces**: `order.create` → `order.call_payment_service` → (propagated to payment-service)
- **Metrics**: `orders_total`, `order_duration_seconds`
- **Propagation**: W3C TraceContext via httpx auto-instrumentation
- Export: OTLP/HTTP → OTel Collector (`OTEL_EXPORTER_OTLP_ENDPOINT`)

## Flow

```
Client  →  POST /api/v1/orders  →  order-service
                                      │  (span: order.create)
                                      │  HTTP POST + traceparent header
                                      ▼
                                 payment-service
                                      │  (span: payment.process)
                                      ▼
                                 Order status = paid | payment_failed
```

## Run locally

```bash
# Terminal 1 – payment-service
cd services/payment-service && cp .env.example .env
uvicorn app.main:app --reload --port 8002

# Terminal 2 – order-service
cd services/order-service && cp .env.example .env
# PAYMENT_SERVICE_URL=http://localhost:8002
uvicorn app.main:app --reload --port 8001
```

## Try it

```bash
curl -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "cust-42",
    "items": [{"sku": "SKU-001", "name": "Widget", "quantity": 2, "unit_price": 19.99}]
  }'
```

Then open Grafana → Explore → Tempo and search for service `order-service`.
