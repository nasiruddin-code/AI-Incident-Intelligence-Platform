#!/usr/bin/env python3
"""End-to-end validation of the local POC (standard library only).

Prerequisites (see docs/POC_GUIDE.md):
    docker compose up -d --build
    docker compose -f demo-apps/docker-compose.yml up -d --build

    python scripts/poc_demo.py              # all scenarios, incl. auto-resolve + recurrence (~6 min)
    python scripts/poc_demo.py --quick      # detection + report emails only (~2 min)

Scenarios (none of them changes the watched applications' code):
  1. independent app, container stdout : catalog-service "database down"
  2. independent app, log file         : legacy-billing.log gets payment errors
  3. our own project, container stdout : payment-service failures (fault injection)
  4. recovery                          : errors stop -> incident auto-resolved -> "Resolved" email
  5. recurrence                        : same failure again -> new incident flagged "recurring"
Each step checks the Mailpit inbox (http://localhost:8025) for the expected email.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AI = "http://localhost:8000/api/v1"
MAILPIT = "http://localhost:8025/api/v1"
CATALOG = "http://localhost:8090"
ORDERS = "http://localhost:8001/api/v1"
PAYMENTS = "http://localhost:8002/api/v1"
LOKI = "http://localhost:3100"
LOG_DIR = Path(__file__).resolve().parent.parent / "demo-apps" / "logs"

results = []


def http(method: str, url: str, body=None, timeout: float = 15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw and resp.headers.get_content_type() == "application/json" else raw.decode()


def step(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}", flush=True)
    return ok


def emails():
    return http("GET", f"{MAILPIT}/messages?limit=200")["messages"]


_seen: set = set()  # emails already present before the current scenario


def mark_seen() -> None:
    """Only emails arriving after this call can satisfy the next checks."""
    _seen.update(m["ID"] for m in emails())


def wait_for_email(predicate, timeout: float, label: str):
    print(f"  ... waiting up to {int(timeout)}s for: {label}", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for m in emails():
            if m["ID"] not in _seen and predicate(m["Subject"]):
                return m
        time.sleep(5)
    return None


def report_text(message_id: str) -> str:
    return http("GET", f"{MAILPIT}/message/{message_id}")["Text"]


def preflight() -> bool:
    print("Preflight")
    ok = True
    for name, url in [("ai-service ready", f"{AI}/health/ready"), ("Mailpit", f"{MAILPIT}/messages?limit=1"),
                      ("Loki", f"{LOKI}/ready"), ("catalog-service (independent app)", f"{CATALOG}/health")]:
        try:
            http("GET", url, timeout=5)
            ok &= step(name, True)
        except (urllib.error.URLError, OSError) as exc:
            ok &= step(name, False, f"{url}: {exc}")
    if ok:
        status = http("GET", f"{AI}/log-monitor/status")
        ok &= step("log monitor running", status["running"], f"query {status['query'][:40]}...")
    return ok


def scenario_catalog_db(timeout: float):
    print("\n1. Independent app (container stdout): catalog-service database down")
    mark_seen()
    http("POST", f"{CATALOG}/chaos?mode=db_down")
    created = wait_for_email(lambda s: "New incident" in s and "catalog-service" in s and "Database" in s,
                             timeout, "New incident email")
    step("incident detected + 'New incident' email", created is not None, created and created["Subject"])
    report = wait_for_email(lambda s: "Incident report" in s and "catalog-service" in s, timeout,
                            "Incident report email")
    if step("'Incident report' email (RCA + remediation)", report is not None, report and report["Subject"]):
        text = report_text(report["ID"])
        for section in ("PROBABLE ROOT CAUSE", "SUGGESTED RESOLUTION", "POTENTIAL IMPACT", "KEY EVIDENCE"):
            step(f"report has {section.title()}", section in text)
        step("only one catalog incident (5xx symptoms merged)",
             sum("New incident" in m["Subject"] and "catalog-service" in m["Subject"] for m in emails()) == 1)
    return report


def scenario_file(timeout: float):
    print("\n2. Independent app (log file): legacy-billing.log")
    mark_seen()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "legacy-billing.log", "a", encoding="utf-8") as f:
        for i in range(4):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            f.write(f"[{now}] ERROR Payment gateway rejected batch charge: card network unavailable (invoice INV-{9000 + i})\n")
    created = wait_for_email(lambda s: "New incident" in s and "legacy-billing" in s, timeout, "legacy-billing incident")
    step("file-based app detected + email", created is not None, created and created["Subject"])


def scenario_own_project(timeout: float):
    print("\n3. Our own project (container stdout): payment-service failures")
    mark_seen()
    http("PUT", f"{PAYMENTS}/fault-injection", {"failure_rate": 1.0, "latency_ms": 0})
    try:
        for i in range(6):
            http("POST", f"{ORDERS}/orders", {"customer_id": f"poc-{i}", "items": [
                {"sku": "SKU-1", "name": "Widget", "quantity": 1, "unit_price": 9.99}]})
    finally:
        http("DELETE", f"{PAYMENTS}/fault-injection")
    created = wait_for_email(lambda s: "New incident" in s and "payment-service" in s and "[Logs]" in s,
                             timeout, "payment-service incident")
    step("own-project failure detected + email", created is not None, created and created["Subject"])


def scenario_resolve_and_recur(timeout: float):
    print("\n4. Recovery: errors stop -> auto-resolve")
    mark_seen()
    http("POST", f"{CATALOG}/chaos?mode=ok")
    resolve_after = int(http("GET", f"{AI}/log-monitor/status").get("interval_seconds", 20))
    resolved = wait_for_email(lambda s: s.split("] ", 1)[-1].startswith("Resolved") and "catalog-service" in s,
                              timeout + 240 + resolve_after, "Resolved email (after the quiet period)")
    step("auto-resolved + 'Resolved' email", resolved is not None, resolved and resolved["Subject"])

    print("\n5. Recurrence: same failure again")
    mark_seen()
    before = recurring_count()
    http("POST", f"{CATALOG}/chaos?mode=db_down")
    again = wait_for_email(lambda s: "New incident" in s and "catalog-service" in s and "recurring" in s,
                           timeout, "recurring incident")
    step("recurring pattern flagged", again is not None, again and again["Subject"])
    after = recurring_count()
    step("recurrence count increased", after > before, f"{before} -> {after}")
    http("POST", f"{CATALOG}/chaos?mode=ok")


def recurring_count() -> int:
    """Highest recurring_count among catalog-service log-monitor incidents."""
    counts = [0]
    for item in http("GET", f"{AI}/incidents/summaries?limit=200")["incidents"]:
        if item["title"].startswith("[Logs] catalog-service"):
            meta = http("GET", f"{AI}/incidents/{item['incident_id']}")["metadata"].get("log_monitor") or {}
            counts.append(int(meta.get("recurring_count") or 0))
    return max(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true", help="skip auto-resolve and recurrence")
    parser.add_argument("--timeout", type=float, default=150, help="seconds to wait for each email")
    parser.add_argument("--keep-inbox", action="store_true", help="don't clear Mailpit first")
    args = parser.parse_args()

    if not preflight():
        print("\nPreflight failed - is the stack up? See docs/POC_GUIDE.md")
        return 2
    if not args.keep_inbox:
        http("DELETE", f"{MAILPIT}/messages")
    http("POST", f"{CATALOG}/chaos?mode=ok")
    try:
        report = scenario_catalog_db(args.timeout)
        scenario_file(args.timeout)
        scenario_own_project(args.timeout)
        if not args.quick:
            scenario_resolve_and_recur(args.timeout)
    finally:
        try:
            http("POST", f"{CATALOG}/chaos?mode=ok")
        except OSError:
            pass

    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed. Inbox: http://localhost:8025  UI: http://localhost:8080")
    if report:
        print("\n--- Incident report (plain-text part) ---\n" + report_text(report["ID"]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
