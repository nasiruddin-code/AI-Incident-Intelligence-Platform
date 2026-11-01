import { api, ApiError, getApiKey, setApiKey, streamInvestigation } from "./api.js";
import { h, replace } from "./dom.js";
import { buildTimeline, formatTime, percent, severityClass, truncate } from "./format.js";

const $ = (sel) => document.querySelector(sel);
const LIST_REFRESH_MS = 15000;

const state = {
  filters: { status: "", severity: "" },
  list: [],
  total: 0,
  selectedId: null,
  incident: null,
  tab: "overview",
  // Results returned by actions in this session (full objects; the incident
  // itself only stores headline fields of RCA / runbook).
  results: {},
  timeline: { kinds: new Set(["log", "span", "metric"]), errorsOnly: false },
  agent: { controller: null, running: false, events: [], result: null, error: null },
};

// ---------------------------------------------------------------- utilities

function toast(message, kind = "info") {
  const el = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status", text: message });
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 4000);
}

function badge(text, cls) {
  return h("span", { class: `badge ${cls || ""}`, text });
}

function sevBadge(sev) {
  return badge(sev, severityClass(sev));
}

function statusBadge(status) {
  return badge(status, `status-${String(status).toLowerCase()}`);
}

function card(title, ...body) {
  return h("section", { class: "card" }, h("h3", { text: title }), ...body);
}

function empty(text) {
  return h("p", { class: "muted", text });
}

function kv(rows) {
  return h("dl", { class: "kv" }, rows.filter(Boolean).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
}

async function guarded(button, fn) {
  if (button) {
    button.disabled = true;
    button.classList.add("busy");
  }
  try {
    return await fn();
  } catch (err) {
    toast(err instanceof ApiError ? err.message : String(err), "error");
    return undefined;
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove("busy");
    }
  }
}

// ---------------------------------------------------------------- health

async function refreshHealth() {
  const pill = $("#health");
  try {
    const r = await api.ready();
    pill.className = `pill ${r.status === "ready" ? "pill-ok" : "pill-warn"}`;
    pill.textContent = `${r.status} · AI: ${r.ai_provider} · DB: ${r.database}`;
  } catch (err) {
    pill.className = "pill pill-bad";
    pill.textContent = err instanceof ApiError && err.status ? `API ${err.status}` : "API unreachable";
  }
}

// ---------------------------------------------------------------- list

async function loadList() {
  try {
    const r = await api.summaries({ ...state.filters, limit: 100 });
    state.list = r.incidents;
    state.total = r.total;
    renderList();
  } catch (err) {
    replace($("#incident-list"), h("li", { class: "error-text", text: err.message }));
  }
}

function renderList() {
  const list = $("#incident-list");
  $("#list-count").textContent = `${state.list.length} of ${state.total}`;
  if (!state.list.length) {
    replace(list, h("li", { class: "muted pad", text: "No incidents match these filters." }));
    return;
  }
  replace(list, state.list.map((i) =>
    h("li", {},
      h("a", {
        href: `#/incident/${encodeURIComponent(i.incident_id)}`,
        class: `list-item ${i.incident_id === state.selectedId ? "active" : ""}`,
        "aria-current": i.incident_id === state.selectedId ? "page" : null,
      },
      h("div", { class: "list-title", text: i.title }),
      h("div", { class: "list-meta" },
        sevBadge(i.severity), statusBadge(i.status),
        h("span", { class: "muted small", text: (i.affected_services || []).join(", ") })),
      i.evidence_summary ? h("div", { class: "muted small clamp", text: i.evidence_summary }) : null),
    )));
}

// ---------------------------------------------------------------- detail

async function selectIncident(id, { keepTab = false } = {}) {
  if (id !== state.selectedId) {
    stopAgent();
    state.results = {};
    state.agent = { controller: null, running: false, events: [], result: null, error: null };
    if (!keepTab) state.tab = "overview";
  }
  state.selectedId = id;
  renderList();
  if (!id) {
    state.incident = null;
    renderDetail();
    return;
  }
  try {
    state.incident = await api.incident(id);
  } catch (err) {
    state.incident = null;
    replace($("#detail"), h("div", { class: "placeholder" }, h("p", { class: "error-text", text: err.message })));
    return;
  }
  renderDetail();
}

async function reloadIncident() {
  if (!state.selectedId) return;
  state.incident = await api.incident(state.selectedId);
  renderDetail();
  loadList();
}

const ACTIONS = [
  { name: "enrich", label: "Pull live telemetry", hint: "Loki, Prometheus and Tempo evidence for the incident window" },
  { name: "baseline", label: "Baseline check", hint: "Compare services with their last hour" },
  { name: "correlate", label: "Correlate", hint: "Link evidence, detect anomalies, score severity" },
  { name: "rca", label: "Root cause (AI)", hint: "AI root-cause analysis" },
  { name: "runbook", label: "Runbook (AI)", hint: "AI runbook and remediation" },
];

async function runAction(name, button) {
  const id = state.selectedId;
  const result = await guarded(button, () => api.action(id, name));
  if (result === undefined) return;
  state.results[name] = result;
  const messages = {
    enrich: `Added ${result.logs_added} logs, ${result.metrics_added} metrics, ${result.spans_added} spans`,
    baseline: `${result.anomalies} anomalous signal(s) of ${result.signals.length}`,
    correlate: `Severity ${result.severity.level} (${result.severity.score}/100), ${result.anomalies.length} anomalies`,
    rca: `Root cause: ${truncate(result.rca?.primary_cause, 90)}`,
    runbook: `Runbook: ${result.runbook?.title}`,
  };
  toast(messages[name] || "Done", "ok");
  if (name === "baseline" || name === "correlate") setTab("analysis", { render: false });
  await reloadIncident();
}

async function setStatus(status, button) {
  const r = await guarded(button, () => api.setStatus(state.selectedId, status));
  if (r) {
    toast(`Status set to ${status}`, "ok");
    await reloadIncident();
  }
}

function renderDetail() {
  const root = $("#detail");
  const inc = state.incident;
  if (!inc) {
    replace(root, h("div", { class: "placeholder" },
      h("h2", { text: "Select an incident" }),
      h("p", { class: "muted", text: "Pick one from the list, or start a failure scenario with “New incident”." })));
    return;
  }

  const resolved = ["RESOLVED", "CLOSED"].includes(inc.status);
  const header = h("header", { class: "detail-header" },
    h("div", { class: "detail-title-row" },
      h("h2", { text: inc.title }),
      h("div", { class: "badges" }, sevBadge(inc.severity), statusBadge(inc.status))),
    h("div", { class: "detail-meta muted small" },
      h("span", { text: `ID ${inc.incident_id}` }),
      h("span", { text: `v${inc.version}` }),
      h("span", { text: `${formatTime(inc.time_window.start)} → ${formatTime(inc.time_window.end)}` }),
      h("span", {}, ...(inc.affected_services || []).map((s) => h("span", { class: "chip", text: s })))),
    h("div", { class: "actions", role: "toolbar", "aria-label": "Incident actions" },
      ...ACTIONS.map((a) => {
        const b = h("button", { type: "button", class: "btn", title: a.hint, text: a.label });
        b.addEventListener("click", () => runAction(a.name, b));
        return b;
      }),
      (() => {
        const b = h("button", {
          type: "button", class: `btn ${resolved ? "" : "btn-primary"}`,
          text: resolved ? "Reopen" : "Resolve",
        });
        b.addEventListener("click", () => setStatus(resolved ? "INVESTIGATING" : "RESOLVED", b));
        return b;
      })()));

  const tabs = [["overview", "Overview"], ["evidence", "Evidence"], ["analysis", "Analysis"], ["agent", "AI agent"]];
  const tabBar = h("nav", { class: "tabs", role: "tablist" }, tabs.map(([key, label]) =>
    h("button", {
      type: "button", role: "tab", class: `tab ${state.tab === key ? "active" : ""}`,
      "aria-selected": state.tab === key ? "true" : "false", text: label,
      onclick: () => { setTab(key); },
    })));

  const views = { overview: renderOverview, evidence: renderEvidence, analysis: renderAnalysis, agent: renderAgent };
  replace(root, header, tabBar, h("div", { class: "tab-body", role: "tabpanel" }, views[state.tab](inc)));
}

// ---------------------------------------------------------------- overview

function renderOverview(inc) {
  const m = inc.metadata || {};
  const rcaFull = state.results.rca;
  const rbFull = state.results.runbook;

  const evidence = card("Evidence",
    h("div", { class: "stats" },
      stat("Logs", inc.logs.length), stat("Metrics", inc.metrics.length), stat("Spans", inc.traces.length),
      stat("Severity score", m.severity_score ?? "—")),
    inc.evidence_summary ? h("p", { class: "muted small", text: inc.evidence_summary }) : null);

  const rca = card("Root cause",
    m.rca_primary_cause
      ? h("div", {},
        h("p", { class: "lead", text: m.rca_primary_cause }),
        kv([
          ["Confidence", percent(m.rca_confidence)],
          ["Answered by", `${m.rca_provider || "—"}${m.rca_degraded ? " (fallback — placeholder text)" : ""}`],
        ]),
        rcaFull ? rcaDetails(rcaFull) : null)
      : empty("No RCA yet — run “Root cause (AI)”."));

  const runbook = card("Runbook",
    rbFull ? runbookDetails(rbFull)
      : m.runbook_title
        ? h("div", {}, h("p", { class: "lead", text: m.runbook_title }),
          kv([["Confidence", percent(m.runbook_confidence)], ["Remediation actions", m.remediation_count ?? "—"]]),
          h("p", { class: "muted small", text: "Generate again to see the full steps." }))
        : empty("No runbook yet — run “Runbook (AI)”."));

  const auto = m.auto_investigation
    ? card("Automatic investigation",
      h("p", { class: "muted small", text: `Finished ${formatTime(m.auto_investigation.finished_at)}` }),
      h("ul", { class: "steps" }, pipelineOrder(m.auto_investigation.steps).map(([k, v]) =>
        h("li", { class: v === "ok" ? "ok" : "fail" }, h("strong", { text: k }), " ", h("span", { text: v })))))
    : null;

  const notes = inc.notes ? card("Notes", h("pre", { class: "notes", text: inc.notes })) : null;
  return h("div", { class: "grid" }, evidence, rca, runbook, auto, notes);
}

// Postgres JSONB does not keep key order, so show steps in pipeline order.
const PIPELINE = ["enrich", "baseline", "correlate", "rca", "runbook"];
function pipelineOrder(steps = {}) {
  const rank = (k) => (PIPELINE.indexOf(k) === -1 ? PIPELINE.length : PIPELINE.indexOf(k));
  return Object.entries(steps).sort(([a], [b]) => rank(a) - rank(b));
}

function stat(label, value) {
  return h("div", { class: "stat" }, h("div", { class: "stat-value", text: String(value) }), h("div", { class: "muted small", text: label }));
}

function rcaDetails(r) {
  return h("div", { class: "subsection" },
    h("p", { text: r.rca.summary }),
    h("h4", { text: "Hypotheses" }),
    h("ol", {}, r.rca.hypotheses.map((x) =>
      h("li", {}, h("strong", { text: x.cause }), ` — ${percent(x.confidence)}`,
        x.affected_component ? h("span", { class: "chip", text: x.affected_component }) : null))),
    r.rca.recommended_next_steps?.length
      ? h("div", {}, h("h4", { text: "Next steps" }), h("ul", {}, r.rca.recommended_next_steps.map((s) => h("li", { text: s }))))
      : null,
    r.similar_incidents?.length
      ? h("p", { class: "muted small", text: `Context from ${r.similar_incidents.length} similar past incident(s).` })
      : null);
}

function runbookDetails(r) {
  const rb = r.runbook;
  return h("div", {},
    h("p", { class: "lead", text: rb.title }),
    h("p", { text: rb.summary }),
    h("ol", { class: "runbook" }, rb.steps.map((s) =>
      h("li", {},
        h("div", { text: s.action }),
        s.command_or_check ? h("code", { text: s.command_or_check }) : null,
        s.expected_result ? h("div", { class: "muted small", text: `Expect: ${s.expected_result}` }) : null,
        s.rollback ? h("div", { class: "muted small", text: `Rollback: ${s.rollback}` }) : null))),
    rb.verification_checks?.length
      ? h("div", {}, h("h4", { text: "Verify" }), h("ul", {}, rb.verification_checks.map((c) => h("li", { text: c }))))
      : null,
    rb.escalation_guidance ? h("p", { class: "muted small", text: `Escalation: ${rb.escalation_guidance}` }) : null,
    r.degraded ? h("p", { class: "warn-text", text: "Produced by the fallback provider — placeholder text." }) : null);
}

// ---------------------------------------------------------------- evidence

function renderEvidence(inc) {
  const { kinds, errorsOnly } = state.timeline;
  const toggles = h("div", { class: "filters" },
    ...[["log", "Logs"], ["span", "Spans"], ["metric", "Metrics"]].map(([k, label]) =>
      h("label", { class: "check" },
        h("input", {
          type: "checkbox", checked: kinds.has(k),
          onchange: (e) => { e.target.checked ? kinds.add(k) : kinds.delete(k); renderDetail(); },
        }), label)),
    h("label", { class: "check" },
      h("input", {
        type: "checkbox", checked: errorsOnly,
        onchange: (e) => { state.timeline.errorsOnly = e.target.checked; renderDetail(); },
      }), "Warnings & errors only"));

  let { items, total } = buildTimeline(inc, { kinds, limit: 2000 });
  if (errorsOnly) items = items.filter((i) => ["WARN", "ERROR", "FATAL"].includes(i.severity));
  const shown = items.slice(0, 500);

  return h("div", {},
    toggles,
    h("p", { class: "muted small", text: `Showing ${shown.length} of ${errorsOnly ? items.length : total} items, newest first.` }),
    shown.length
      ? h("ul", { class: "timeline" }, shown.map((i) =>
        h("li", { class: `tl-item ${severityClass(i.severity)}` },
          h("span", { class: "tl-time mono small", text: formatTime(i.time) }),
          badge(i.kind, `kind-${i.kind}`),
          h("span", { class: "chip", text: i.service || "?" }),
          h("span", { class: "tl-text", text: truncate(i.text, 400) }),
          i.traceId ? h("span", { class: "mono small muted", title: "trace id", text: truncate(i.traceId, 14) }) : null)))
      : empty("No evidence yet. Use “Pull live telemetry” to fetch it from the observability stack."));
}

// ---------------------------------------------------------------- analysis

function renderAnalysis(inc) {
  const corr = state.results.correlate;
  const baseline = inc.metadata?.baseline;

  const correlation = card("Correlation & anomalies",
    corr
      ? h("div", {},
        kv([
          ["Severity", h("span", {}, sevBadge(corr.severity.level), ` ${corr.severity.score}/100`)],
          ["Links", String(corr.links.length)],
          ["Trace IDs", String(corr.correlated_trace_ids.length)],
        ]),
        corr.anomalies.length
          ? h("ul", { class: "anomalies" }, corr.anomalies.map((a) =>
            h("li", {}, h("strong", { text: a.anomaly_type }), a.service ? ` · ${a.service}` : "",
              h("div", { class: "muted small", text: `${a.description} (score ${a.score})` }))))
          : empty("No anomalies detected."))
      : empty("Run “Correlate” to link evidence and score severity."));

  const baselineCard = card("Baseline: compared with each service's last hour",
    baseline
      ? h("div", {},
        h("p", { class: "muted small", text: `Analyzed ${formatTime(baseline.analyzed_at)} · ${baseline.anomalies} anomalous signal(s)` }),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Service", "Signal", "Baseline", "Incident", "z", ""].map((t) => h("th", { scope: "col", text: t })))),
          h("tbody", {}, baseline.signals.map((s) =>
            h("tr", { class: s.anomalous ? "row-anomaly" : "" },
              h("td", { text: s.service }),
              h("td", { text: s.signal }),
              h("td", { class: "num", text: s.baseline_median ?? "—" }),
              h("td", { class: "num", text: s.current_median ?? "—" }),
              h("td", { class: "num", text: s.z_score ?? "—" }),
              h("td", { title: s.reason, text: s.anomalous ? "anomalous" : truncate(s.reason, 40) })))))))
      : empty("Run “Baseline check” to compare against recent history in Prometheus."));

  const similarBody = h("div", {}, empty("Loading…"));
  api.similar(inc.incident_id).then((r) => {
    replace(similarBody, r.similar.length
      ? h("ul", { class: "similar" }, r.similar.map((s) =>
        h("li", {},
          h("a", { href: `#/incident/${encodeURIComponent(s.incident_id)}`, text: s.title }),
          " ", statusBadge(s.status), h("span", { class: "score", text: ` ${Math.round(s.score * 100)}% similar` }),
          s.rca_primary_cause ? h("div", { class: "small", text: `Past cause: ${s.rca_primary_cause}` }) : null,
          s.runbook_title ? h("div", { class: "small", text: `Runbook: ${s.runbook_title}` }) : null,
          h("div", { class: "muted small", text: `Shared: ${s.shared_features.slice(0, 4).join(", ")}` }))))
      : empty("No similar past incidents."));
  }).catch((err) => replace(similarBody, h("p", { class: "error-text", text: err.message })));

  return h("div", { class: "grid" }, correlation, baselineCard, card("Similar past incidents", similarBody));
}

// ---------------------------------------------------------------- agent

function stopAgent() {
  state.agent.controller?.abort();
  state.agent.controller = null;
  state.agent.running = false;
}

function startAgent(question, mode) {
  stopAgent();
  const agent = { controller: null, running: true, events: [], result: null, error: null };
  state.agent = agent;
  const body = { incident_id: state.selectedId, question: question || null };
  if (mode) body.mode = mode;
  agent.controller = streamInvestigation(
    body,
    (ev) => {
      if (state.agent !== agent) return;
      if (ev.event === "result") agent.result = ev.data;
      else if (ev.event === "error") agent.error = ev.data.detail;
      else agent.events.push(ev);
      if (state.tab === "agent") renderAgentLog();
    },
    (err) => {
      if (state.agent !== agent) return;
      agent.running = false;
      if (err) agent.error = err.message;
      if (state.tab === "agent") renderDetail();
      if (agent.result) reloadIncident().catch(() => {});
    });
  renderDetail();
}

function renderAgent() {
  const a = state.agent;
  const question = h("textarea", {
    id: "agent-question", rows: "2", placeholder: "What should the agent find out? e.g. Why are payments failing?",
    "aria-label": "Question for the agent",
  });
  const mode = h("select", { id: "agent-mode", "aria-label": "Agent mode" },
    h("option", { value: "", text: "Auto" }),
    h("option", { value: "planner", text: "Planner (model picks tools)" }),
    h("option", { value: "fixed", text: "Fixed pipeline" }));
  const run = h("button", {
    type: "button", class: "btn btn-primary", text: a.running ? "Running…" : "Investigate", disabled: a.running,
    onclick: () => startAgent(question.value.trim(), mode.value),
  });
  const stop = a.running ? h("button", { type: "button", class: "btn", text: "Stop", onclick: () => { stopAgent(); renderDetail(); } }) : null;

  return h("div", {},
    h("div", { class: "agent-form" }, question, h("div", { class: "row" }, mode, run, stop)),
    h("div", { id: "agent-log", "aria-live": "polite" }, agentLog()));
}

function renderAgentLog() {
  const el = document.getElementById("agent-log");
  if (el) replace(el, agentLog());
}

function agentLog() {
  const a = state.agent;
  const items = [];
  for (const ev of a.events) {
    if (ev.event === "mode") {
      items.push(h("li", { class: "agent-mode muted small", text: `Mode: ${ev.data.mode}${ev.data.reason ? ` — ${ev.data.reason}` : ""}` }));
    } else if (ev.event === "planning") {
      items.push(h("li", { class: "agent-planning muted small", text: `Step ${ev.data.step}/${ev.data.max_steps}: model choosing next tool…` }));
    } else if (ev.event === "step") {
      const s = ev.data;
      items.push(h("li", { class: `agent-step ${s.success ? "ok" : "fail"}` },
        h("div", {}, h("strong", { text: s.tool }), s.input_summary ? h("span", { class: "muted small", text: ` ${s.input_summary}` }) : null),
        s.thought ? h("div", { class: "thought", text: s.thought }) : null,
        h("div", { class: "mono small muted", text: truncate(s.output_summary, 300) })));
    }
  }
  if (a.running) items.push(h("li", { class: "agent-running muted small", text: "Working…" }));
  const parts = [items.length ? h("ol", { class: "agent-steps" }, items) : empty("Ask a question and start an investigation; each step appears here as it runs.")];
  if (a.error) parts.push(h("p", { class: "error-text", text: a.error }));
  if (a.result) {
    parts.push(card("Answer", h("p", { class: "answer", text: a.result.summary }),
      h("p", { class: "muted small", text: `${a.result.steps.length} steps · mode ${a.result.mode} · ${a.result.provider}` })));
  }
  return h("div", {}, ...parts);
}

// ---------------------------------------------------------------- dialogs

function setupNewIncidentDialog() {
  const dialog = $("#new-dialog");
  $("#new-btn").addEventListener("click", () => dialog.showModal());
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());

  $("#scenario-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const btn = e.target.querySelector("button[type=submit]");
    const r = await guarded(btn, () => api.runScenario({
      scenario: f.get("scenario"), order_count: Number(f.get("order_count")),
    }));
    if (r) {
      dialog.close();
      toast(`${r.orders_failed} of ${r.orders_attempted} orders failed`, "ok");
      location.hash = `#/incident/${encodeURIComponent(r.incident_id)}`;
      loadList();
    }
  });

  $("#blank-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const btn = e.target.querySelector("button[type=submit]");
    const services = String(f.get("services") || "").split(",").map((s) => s.trim()).filter(Boolean);
    const r = await guarded(btn, () => api.create({
      title: f.get("title"), severity: f.get("severity"), affected_services: services,
    }));
    if (r) {
      dialog.close();
      e.target.reset();
      location.hash = `#/incident/${encodeURIComponent(r.incident_id)}`;
      loadList();
    }
  });
}

function setupSettingsDialog() {
  const dialog = $("#settings-dialog");
  const input = $("#api-key");
  $("#settings-btn").addEventListener("click", () => {
    input.value = getApiKey();
    dialog.showModal();
  });
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  $("#settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    setApiKey(input.value.trim());
    dialog.close();
    toast(input.value.trim() ? "API key saved in this browser" : "API key cleared", "ok");
    refreshHealth();
    loadList();
    if (state.selectedId) selectIncident(state.selectedId, { keepTab: true });
  });
}

// ---------------------------------------------------------------- routing / boot

const TABS = ["overview", "evidence", "analysis", "agent"];

function incidentHash(id, tab) {
  return `#/incident/${encodeURIComponent(id)}${tab && tab !== "overview" ? `/${tab}` : ""}`;
}

function setTab(tab, { render = true } = {}) {
  state.tab = tab;
  // replaceState: switching tabs shouldn't fill the back-button history
  history.replaceState(null, "", incidentHash(state.selectedId, tab));
  if (render) renderDetail();
}

function route() {
  const m = location.hash.match(/^#\/incident\/([^/]+)(?:\/(\w+))?$/);
  if (!m) return selectIncident(null);
  const id = decodeURIComponent(m[1]);
  const tab = TABS.includes(m[2]) ? m[2] : "overview";
  if (id === state.selectedId) {
    if (tab !== state.tab) { state.tab = tab; renderDetail(); }
    return;
  }
  state.tab = tab;
  selectIncident(id, { keepTab: true });
}

function setupFilters() {
  for (const id of ["status", "severity"]) {
    $(`#filter-${id}`).addEventListener("change", (e) => {
      state.filters[id] = e.target.value;
      loadList();
    });
  }
  $("#refresh-btn").addEventListener("click", () => { loadList(); refreshHealth(); });
}

function boot() {
  setupFilters();
  setupNewIncidentDialog();
  setupSettingsDialog();
  window.addEventListener("hashchange", route);
  refreshHealth();
  loadList();
  route();
  setInterval(() => {
    if (document.visibilityState === "visible") {
      loadList();
      refreshHealth();
    }
  }, LIST_REFRESH_MS);
}

boot();
