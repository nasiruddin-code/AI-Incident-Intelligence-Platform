from fastapi import APIRouter, HTTPException

from app.core.dependencies import LogAnalyzerServiceDep
from app.providers.base import AIProviderError
from app.schemas.log_analysis import LogAnalysisRequest, LogAnalysisResponse

router = APIRouter()


@router.post("/analyze", response_model=LogAnalysisResponse, summary="Analyze a log entry with AI")
async def analyze_log(
    payload: LogAnalysisRequest,
    service: LogAnalyzerServiceDep,
) -> LogAnalysisResponse:
    try:
        return await service.analyze(payload)
    except AIProviderError as exc:
        # Upstream AI provider problem (bad key, outage, malformed response)
        # -> 502, distinct from a validation error on the caller's payload.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
