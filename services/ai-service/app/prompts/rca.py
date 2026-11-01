from app.prompts.base import PromptTemplate

RCA_PROMPT = PromptTemplate(
    name="rca_v1",
    system_instruction=(
        "You are a principal Site Reliability Engineer performing Root Cause Analysis. "
        "You receive a structured Incident Context containing correlated logs, metrics, "
        "traces, anomaly findings, and severity information. "
        "Produce a rigorous, evidence-based RCA. Prefer specific causes over generic ones. "
        "Cite concrete evidence (log messages, span names, metric values, services). "
        "Rank multiple hypotheses when the evidence is ambiguous. "
        "Never invent telemetry that was not provided."
    ),
    template=(
        "Perform Root Cause Analysis on the following incident.\n\n"
        "=== INCIDENT ===\n"
        "ID: {incident_id}\n"
        "Title: {title}\n"
        "Status: {status}\n"
        "Severity: {severity}\n"
        "Affected services: {affected_services}\n"
        "Time window: {time_window}\n"
        "Scenario: {scenario}\n"
        "Evidence summary: {evidence_summary}\n"
        "Notes: {notes}\n"
        "Metadata: {metadata}\n\n"
        "=== LOGS ({log_count}) ===\n"
        "{logs_block}\n\n"
        "=== METRICS ({metric_count}) ===\n"
        "{metrics_block}\n\n"
        "=== TRACES / SPANS ({span_count}) ===\n"
        "{traces_block}\n\n"
        "=== CORRELATION / ANOMALIES (if available) ===\n"
        "{correlation_block}\n\n"
        "Based solely on the evidence above, produce:\n"
        "1. A short executive summary\n"
        "2. The primary (most likely) root cause\n"
        "3. Ranked hypotheses with confidence and supporting evidence quotes\n"
        "4. Contributing factors\n"
        "5. Recommended next steps\n"
        "6. Overall confidence (0–1)\n"
        "7. Evidence gaps (what is missing that would help)\n"
    ),
)
