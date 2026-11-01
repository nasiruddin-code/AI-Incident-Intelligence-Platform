#!/usr/bin/env bash
set -euo pipefail

echo "=== MVP validation ==="
fail=0

check() {
  local name="$1"
  local url="$2"
  if curl -sf --max-time 5 "$url" >/dev/null; then
    echo "OK  $name"
  else
    echo "FAIL $name ($url)"
    fail=1
  fi
}

check "Prometheus" "http://localhost:9090/-/healthy"
check "Loki" "http://localhost:3100/ready"
check "Tempo" "http://localhost:3200/ready"
check "AI health" "http://localhost:8000/api/v1/health"
check "Order health" "http://localhost:8001/api/v1/health"
check "Payment health" "http://localhost:8002/api/v1/health"
check "AI metrics" "http://localhost:8000/metrics"
check "Obs health" "http://localhost:8000/api/v1/observability/health"

echo "--- E2E pipeline ---"
resp=$(curl -sf -X POST http://localhost:8000/api/v1/e2e/run \
  -H "Content-Type: application/json" \
  -d '{"title":"validation-run","check_observability":true,"run_rca":true,"run_runbook":true}' || echo '{"ok":false}')
echo "$resp" | head -c 500
echo
if echo "$resp" | grep -qE '"ok"[[:space:]]*:[[:space:]]*true'; then
  echo "OK  E2E pipeline"
else
  echo "FAIL E2E pipeline"
  fail=1
fi

if [[ $fail -eq 0 ]]; then
  echo "=== ALL CHECKS PASSED ==="
  exit 0
else
  echo "=== SOME CHECKS FAILED ==="
  exit 1
fi
