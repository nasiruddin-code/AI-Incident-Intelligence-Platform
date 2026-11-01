"""
Business logic for log analysis. This layer exists so endpoints stay thin —
they just do request validation and status codes — and so this logic is
reusable later from, e.g., the Week 8+ Incident Context builder or the
Week 12 LangGraph agent's tools, without depending on FastAPI at all.
"""
import uuid

from app.prompts.log_analysis import LOG_ANALYSIS_PROMPT
from app.providers.base import AIProvider
from app.schemas.log_analysis import LogAnalysisRequest, LogAnalysisResponse, LogAnalysisResult


class LogAnalyzerService:
    def __init__(self, provider: AIProvider):
        self._provider = provider

    async def analyze(self, request: LogAnalysisRequest) -> LogAnalysisResponse:
        prompt = LOG_ANALYSIS_PROMPT.render(
            log_content=request.log_content,
            service_name=request.service_name or "unspecified",
            context=request.context or "none provided",
        )

        result, info = await self._provider.generate_structured_with_info(
            prompt=prompt,
            response_model=LogAnalysisResult,
            system_instruction=LOG_ANALYSIS_PROMPT.system_instruction,
        )

        return LogAnalysisResponse(
            request_id=str(uuid.uuid4()),
            provider=info.provider,
            degraded=info.degraded,
            analysis=result,
        )
