# Observability Stack (Week 5)

Local, development-grade observability infrastructure for the AI Observability Platform.

## Components

| Component        | Image                                      | Host Port | Purpose                                      |
|------------------|--------------------------------------------|-----------|----------------------------------------------|
| OTel Collector   | `otel/opentelemetry-collector-contrib`     | 4317/4318 | Central pipeline – receives OTLP, routes data |
| Prometheus       | `prom/prometheus`                          | 9090      | Metrics storage & querying                   |
| Loki             | `grafana/loki`                             | 3100      | Log aggregation                              |
| Tempo            | `grafana/tempo`                            | 3200      | Distributed tracing backend                  |
| Grafana          | `grafana/grafana`                          | 3000      | Visualization + pre-provisioned datasources  |

## Architecture

```
Applications (ai-service, later order/payment)
        │
        │  OTLP (gRPC :4317 / HTTP :4318)
        ▼
┌───────────────────┐
│  OTel Collector   │
│  (batch + route)  │
└─────────┬─────────┘
          │
    ┌─────┼──────────────┐
    ▼     ▼              ▼
Prometheus  Loki       Tempo
 (metrics) (logs)     (traces)
    │       │            │
    └───────┴────────────┘
              │
              ▼
           Grafana
```

## How to run

From the repository root:

```bash
# Start the full stack (ai-service + observability)
docker compose up -d

# Or start only the observability backends (without building ai-service)
docker compose up -d prometheus loki tempo otel-collector grafana
```

## Verify

```bash
# All containers healthy
docker compose ps

# Prometheus
curl -s http://localhost:9090/-/healthy

# Loki
curl -s http://localhost:3100/ready

# Tempo
curl -s http://localhost:3200/ready

# Grafana (login: admin / admin)
open http://localhost:3000
```

In Grafana go to **Connections → Data sources**.  
Prometheus, Loki and Tempo should appear as green / “Data source is working”.

## Sending test telemetry (optional)

You can push a sample OTLP payload with `curl` or any OTel SDK.  
Example using the collector’s HTTP endpoint:

```bash
# Minimal OTLP/HTTP metrics example (requires a proper protobuf/json payload)
# Prefer using the OpenTelemetry SDK from application code (Week 6-7).
```

## Configuration files

| File | Description |
|------|-------------|
| `otel-collector-config.yaml` | Receivers, processors, exporters & pipelines |
| `prometheus.yml` | Scrape targets (collector + self) |
| `loki-config.yaml` | Single-node Loki with filesystem storage |
| `tempo.yaml` | Single-node Tempo with local blocks |
| `grafana/provisioning/datasources/datasources.yaml` | Auto-provisioned Prometheus / Loki / Tempo |

## Stopping

```bash
docker compose down          # keep volumes
docker compose down -v       # also delete data volumes
```

## Notes for later weeks

- **Week 6–7**: Sample microservices will send real OTLP traffic to the collector.
- **Week 13**: The LangGraph agent will query Prometheus / Loki / Tempo APIs directly.
- **Week 14**: Additional Grafana dashboards for AI insights, RCA and confidence scores will be added under `grafana/provisioning/dashboards/`.


## Application telemetry (Week 7)

Once order-service / payment-service / ai-service are running with
`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`, they export:

| Signal  | Backend   | How to view |
|---------|-----------|-------------|
| Traces  | Tempo     | Grafana → Explore → Tempo → search `order-service` |
| Metrics | Prometheus| Grafana → Explore → Prometheus → `orders_total`, `payments_total` |
| Logs    | Loki      | (structured logs with trace_id when LoggingInstrumentor is active) |

Generate traffic:

```bash
curl -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"cust-1","items":[{"sku":"A","name":"Item","quantity":1,"unit_price":9.99}]}'
```

Then look for the distributed trace spanning `order-service` → `payment-service` in Tempo.


## Grafana AI Insights dashboard (Week 14)

After `docker compose up -d`:

1. Open http://localhost:3000 (admin/admin)
2. Go to **Dashboards → AI Observability → AI Incident Insights**

Panels include:
- Order / payment rates by status
- Open incidents, severity breakdown
- RCA & runbook confidence gauges
- Error logs from Loki

### Prometheus alerts

Rules live in `observability/prometheus/alerts.yml`:
- ServiceDown
- HighPaymentFailureRate
- HighOrderFailureRate
- ElevatedPaymentLatency

View in Prometheus → **Alerts**: http://localhost:9090/alerts
