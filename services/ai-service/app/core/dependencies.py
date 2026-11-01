"""
Central place for FastAPI `Depends(...)` wiring.
"""
from fastapi import Depends
from typing_extensions import Annotated

from app.core.config import Settings, get_settings
from app.providers.base import AIProvider
from app.providers.factory import get_ai_provider
from app.services.log_analyzer import LogAnalyzerService
from app.services.rca_engine import RCAEngine
from app.services.runbook_engine import RunbookEngine

SettingsDep = Annotated[Settings, Depends(get_settings)]
AIProviderDep = Annotated[AIProvider, Depends(get_ai_provider)]


def get_log_analyzer_service(provider: AIProviderDep) -> LogAnalyzerService:
    return LogAnalyzerService(provider=provider)


def get_rca_engine(provider: AIProviderDep) -> RCAEngine:
    return RCAEngine(provider=provider)


def get_runbook_engine(provider: AIProviderDep) -> RunbookEngine:
    return RunbookEngine(provider=provider)


LogAnalyzerServiceDep = Annotated[LogAnalyzerService, Depends(get_log_analyzer_service)]
RCAEngineDep = Annotated[RCAEngine, Depends(get_rca_engine)]
RunbookEngineDep = Annotated[RunbookEngine, Depends(get_runbook_engine)]
