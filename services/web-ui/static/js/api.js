// Thin client for the ai-service API (proxied by nginx at /api).
import { SSEParser, describeError } from "./format.js";

const BASE = "/api/v1";
const KEY_STORAGE = "incident-ui.api-key";

export class ApiError extends Error {
  constructor(status, detail) {
    super(describeError(status, detail));
    this.status = status;
    this.detail = detail;
  }
}

export function getApiKey() {
  try {
    return localStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

export function setApiKey(key) {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* storage unavailable (private mode): key lasts for this page only */
  }
}

function headers(json) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  const key = getApiKey();
  if (key) h["X-API-Key"] = key;
  return h;
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: headers(body !== undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new ApiError(0, err.message);
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = data && typeof data === "object" ? data.detail : data;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

export const api = {
  ready: () => request("GET", "/health/ready"),
  summaries: ({ status = "", severity = "", limit = 50, offset = 0 } = {}) => {
    const q = new URLSearchParams({ limit, offset });
    if (status) q.set("status", status);
    if (severity) q.set("severity", severity);
    return request("GET", `/incidents/summaries?${q}`);
  },
  incident: (id) => request("GET", `/incidents/${encodeURIComponent(id)}`),
  create: (body) => request("POST", "/incidents", body),
  runScenario: (body) => request("POST", "/incidents/scenarios/run", body),
  setStatus: (id, status) =>
    request("PATCH", `/incidents/${encodeURIComponent(id)}/status?new_status=${encodeURIComponent(status)}`),
  action: (id, name, body) => request("POST", `/incidents/${encodeURIComponent(id)}/${name}`, body),
  similar: (id) => request("GET", `/incidents/${encodeURIComponent(id)}/similar?limit=5`),
};

/**
 * Stream an agent investigation. onEvent({event, data}) is called per event.
 * Returns the AbortController (call .abort() to stop).
 */
export function streamInvestigation(body, onEvent, onDone) {
  const controller = new AbortController();
  (async () => {
    try {
      const res = await fetch(`${BASE}/agent/investigate/stream`, {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        let detail = "";
        try {
          detail = (await res.json()).detail;
        } catch {
          /* non-JSON error body */
        }
        throw new ApiError(res.status, detail);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SSEParser();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const ev of parser.feed(decoder.decode(value, { stream: true }))) onEvent(ev);
      }
      onDone(null);
    } catch (err) {
      onDone(err.name === "AbortError" ? null : err);
    }
  })();
  return controller;
}
