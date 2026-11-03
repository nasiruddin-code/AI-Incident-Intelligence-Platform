// Run: node --test services/web-ui/tests
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SSEParser, buildTimeline, describeError, formatTime, meaningfulAttributes, percent, severityClass, truncate,
} from "../static/js/format.js";

test("SSE parser handles events split across chunks, keepalives and CRLF", () => {
  const p = new SSEParser();
  assert.deepEqual(p.feed('event: mode\ndata: {"mode":"fi'), []);
  assert.deepEqual(p.feed('xed"}\n\n: keepalive\n\nevent: step\r\ndata: {"tool":"get_incident"}\r\n\r\n'), [
    { event: "mode", data: { mode: "fixed" } },
    { event: "step", data: { tool: "get_incident" } },
  ]);
  assert.deepEqual(p.feed("event: result\ndata: "), []);
  assert.deepEqual(p.feed('{"summary":"done"}\n\n'), [{ event: "result", data: { summary: "done" } }]);
});

test("SSE parser joins multi-line data and tolerates non-JSON", () => {
  const p = new SSEParser();
  assert.deepEqual(p.feed("data: hello\ndata: world\n\n"), [{ event: "message", data: "hello\nworld" }]);
});

test("timeline merges evidence newest first, filters kinds and limits", () => {
  const res = { service_name: "payment-service" };
  const incident = {
    logs: [{ timestamp: "2026-10-20T10:00:01Z", severity: "ERROR", body: "Payment FAILED", resource: res, trace_id: "t1" }],
    traces: [{ start_time: "2026-10-20T10:00:03Z", status: "ERROR", name: "payment.process", resource: res, trace_id: "t1" }],
    metrics: [{ timestamp: "2026-10-20T10:00:02Z", name: "payments_total", value: 4, attributes: { status: "failed" }, resource: res }],
  };
  const all = buildTimeline(incident);
  assert.deepEqual(all.items.map((i) => i.kind), ["span", "metric", "log"]);
  assert.equal(all.items[0].severity, "ERROR");
  assert.equal(all.items[1].text, "payments_total = 4 {status=failed}");

  const logsOnly = buildTimeline(incident, { kinds: new Set(["log"]) });
  assert.deepEqual(logsOnly.items.map((i) => i.kind), ["log"]);
  assert.equal(buildTimeline(incident, { limit: 1 }).items.length, 1);
  assert.equal(buildTimeline(incident, { limit: 1 }).total, 3);
});

test("formatting helpers", () => {
  assert.equal(severityClass("FATAL"), "sev-fatal");
  assert.equal(formatTime("2026-10-20T10:00:01.123Z"), "2026-10-20 10:00:01Z");
  assert.equal(formatTime("not a date"), "—");
  assert.equal(percent(0.725), "73%");
  assert.equal(percent(undefined), "—");
  assert.equal(truncate("abcdef", 4), "abc…");
});

test("API errors get actionable messages", () => {
  assert.match(describeError(401), /API key/);
  assert.match(describeError(409), /retry/);
  assert.equal(describeError(500, "boom"), "Request failed (500): boom");
  assert.equal(describeError(0, "Failed to fetch"), "Request failed (network error): Failed to fetch");
  assert.match(describeError(502, "<html><body>502 Bad Gateway</body></html>"), /not reachable/);
  assert.doesNotMatch(describeError(500, "<html>boom</html>"), /html/);
});

test("metric attributes drop scrape/process labels", () => {
  const attrs = { status: "failed", job: "otel-collector", telemetry_sdk_version: "1.45", http_route: "/api/v1/orders" };
  assert.deepEqual(meaningfulAttributes(attrs), [["status", "failed"], ["http_route", "/api/v1/orders"]]);
});
