from app.prompts.base import PromptTemplate

RUNBOOK_PROMPT = PromptTemplate(
    name="runbook_v1",
    system_instruction=(
        "You are a principal SRE writing an incident response runbook and remediation plan. "
        "You receive an Incident Context and optionally a prior Root Cause Analysis. "
        "Produce a practical, ordered runbook that an on-call engineer can follow. "
        "Prefer safe, reversible steps first. Mark high-risk actions clearly. "
        "Include verification checks after remediation. "
        "Do not invent infrastructure that was not mentioned in the evidence."
    ),
    template=(
        "Generate an incident response runbook and remediation recommendations.\n\n"
        "=== INCIDENT ===\n"
        "ID: {incident_id}\n"
        "Title: {title}\n"
        "Severity: {severity}\n"
        "Affected services: {affected_services}\n"
        "Scenario: {scenario}\n"
        "Evidence summary: {evidence_summary}\n"
        "Notes: {notes}\n\n"
        "=== PRIOR RCA (if any) ===\n"
        "{rca_block}\n\n"
        "=== KEY EVIDENCE ===\n"
        "Logs:\n{logs_block}\n\n"
        "Metrics:\n{metrics_block}\n\n"
        "Traces:\n{traces_block}\n\n"
        "Produce:\n"
        "1. A clear runbook title and situation summary\n"
        "2. Ordered steps (action, optional command/check, expected result, rollback)\n"
        "3. Prioritized remediation actions with risk level and confidence\n"
        "4. Post-fix verification checks\n"
        "5. Escalation guidance\n"
        "6. Overall confidence (0–1) and estimated time in minutes\n"
    ),
)
