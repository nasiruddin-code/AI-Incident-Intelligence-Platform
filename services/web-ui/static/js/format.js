// Pure helpers (no DOM) — unit-tested with `node --test` (see tests/).

export const SEVERITY_ORDER = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"];

// Scrape/process labels that say where a metric came from, not what it measures.
const INFRA_LABELS = new Set([
  "job", "instance", "service", "exported_job", "exported_instance", "service_name", "service_version",
  "service_instance_id", "deployment_environment", "url_scheme", "network_protocol_version",
]);

export function meaningfulAttributes(attrs = {}) {
  return Object.entries(attrs).filter(([k]) => !INFRA_LABELS.has(k) && !k.startsWith("telemetry_sdk_"));
}

export function severityClass(severity) {
  return `sev-${String(severity || "info").toLowerCase()}`;
}

export function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function truncate(text, max) {
  const s = String(text ?? "");
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

export function percent(x) {
  return typeof x === "number" ? `${Math.round(x * 100)}%` : "—";
}

/**
 * One chronological list of an incident's evidence, newest first.
 * kinds: optional Set of "log" | "span" | "metric" to keep.
 */
export function buildTimeline(incident, { kinds = null, limit = 500 } = {}) {
  const items = [];
  const want = (k) => !kinds || kinds.has(k);
  if (want("log")) {
    for (const l of incident.logs || []) {
      items.push({ kind: "log", time: l.timestamp, severity: l.severity, service: l.resource?.service_name,
                   text: l.body, traceId: l.trace_id });
    }
  }
  if (want("span")) {
    for (const s of incident.traces || []) {
      items.push({ kind: "span", time: s.start_time, severity: s.status === "ERROR" ? "ERROR" : "INFO",
                   service: s.resource?.service_name, text: `${s.name} · ${s.status}`, traceId: s.trace_id });
    }
  }
  if (want("metric")) {
    for (const m of incident.metrics || []) {
      const attrs = meaningfulAttributes(m.attributes).map(([k, v]) => `${k}=${v}`).join(", ");
      items.push({ kind: "metric", time: m.timestamp, severity: "INFO", service: m.resource?.service_name,
                   text: `${m.name} = ${m.value}${attrs ? ` {${attrs}}` : ""}` });
    }
  }
  items.sort((a, b) => new Date(b.time) - new Date(a.time));
  return { items: items.slice(0, limit), total: items.length };
}

/**
 * Incremental parser for a text/event-stream body.
 * feed(chunk) returns the complete events seen so far as {event, data}.
 * Comment lines (": keepalive") are ignored; data is JSON-decoded.
 */
export class SSEParser {
  constructor() {
    this.buffer = "";
  }

  feed(chunk) {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events = [];
    let end;
    while ((end = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const i = line.indexOf(":");
        const field = i === -1 ? line : line.slice(0, i);
        const value = i === -1 ? "" : line.slice(i + 1).replace(/^ /, "");
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
      if (data.length) {
        let parsed;
        try {
          parsed = JSON.parse(data.join("\n"));
        } catch {
          parsed = data.join("\n");
        }
        events.push({ event, data: parsed });
      }
    }
    return events;
  }
}

/** Human message for an API failure. */
export function describeError(status, detail) {
  const hints = {
    401: "Authentication required — set an API key in Settings.",
    403: "Your API key does not allow this action.",
    409: "The incident changed at the same time — please retry.",
    429: "Rate limit reached — wait a moment and retry.",
    502: "ai-service is not reachable (it may be restarting) — retry in a few seconds.",
    503: "ai-service is unavailable — retry in a few seconds.",
    504: "ai-service took too long to answer — retry, or check its logs.",
  };
  // Proxy error pages are HTML: never show their markup as the message
  if (typeof detail === "string" && /^\s*</.test(detail)) detail = "";
  const base = hints[status] || `Request failed (${status || "network error"})`;
  return detail && !hints[status] ? `${base}: ${detail}` : base;
}
