from app.prompts.base import PromptTemplate

LOG_ANALYSIS_PROMPT = PromptTemplate(
    name="log_analysis_v1",
    system_instruction=(
        "You are an expert Site Reliability Engineer performing rapid triage. "
        "You analyze raw application/system log content and return a concise, "
        "structured assessment. Be specific and avoid generic filler."
    ),
    template=(
        "Analyze the following log content and assess it.\n\n"
        "Service / context: {service_name}\n"
        "Additional context: {context}\n\n"
        "Log content:\n"
        "---\n"
        "{log_content}\n"
        "---\n\n"
        "Determine: severity, a short category label, a one-to-two sentence "
        "summary, the probable root cause, a recommended next action, and your "
        "confidence (0 to 1) in this assessment."
    ),
)
