# web-ui

Browser UI for the incident intelligence platform: http://localhost:8080
(after `docker compose up -d --build`).

- **Incident list** — filter by status / severity, auto-refreshes every 15 s.
- **New incident** — run a failure scenario (payment-service faults are
  injected and restored automatically) or open an empty incident.
- **Overview** — evidence counts, root cause, runbook, automatic
  (Alertmanager-triggered) investigation steps, notes.
- **Actions** — pull live telemetry, baseline check, correlate, root cause
  (AI), runbook (AI), resolve / reopen.
- **Evidence** — one timeline of logs, spans and metrics (filters by kind and
  warnings/errors).
- **Analysis** — correlation anomalies, baseline signals vs. the last hour,
  similar past incidents with their causes and runbooks.
- **AI agent** — ask a question; each step (tool, the model's reasoning,
  result) appears live as the investigation runs, then the answer.

Links are shareable: `#/incident/<id>` and `#/incident/<id>/<tab>`.

## How it is built

Static files (`static/`) served by nginx, which also proxies `/api/` to
ai-service — one origin, no CORS, and the agent's server-sent event stream is
passed through unbuffered (`nginx.conf`). No build step and no third-party
code: plain ES modules.

- `js/format.js` — pure helpers (SSE parsing, evidence timeline, formatting),
  unit-tested with Node
- `js/api.js` — API client and the streaming reader
- `js/dom.js` — tiny DOM builder; all incident data is inserted as text
  (`textContent`), never as HTML, so log lines cannot inject markup
- `js/app.js` — views and state

nginx sends a strict Content-Security-Policy (`script-src 'self'`, no inline
script) plus `nosniff` and `no-referrer`.

**Authentication.** If ai-service runs with `ENABLE_AUTH=true`, open
*Settings* and enter an API key; it is kept in this browser's local storage
and sent as `X-API-Key`. Give the UI a key with the scopes it needs: `read`
to browse, `write` for actions, `ai` for RCA / runbook / agent.

## Develop

```bash
node --test "tests/*.test.mjs"          # unit tests
for f in static/js/*.js; do node --check "$f"; done
docker compose up -d --build web-ui      # rebuild after changes
```
