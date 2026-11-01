"""Runtime failure-injection controls, used by ai-service incident scenarios.

Disabled (404) in production unless FAULT_INJECTION_API_ENABLED=true.
If FAULT_INJECTION_TOKEN is set, requests must send it as X-Fault-Token.
"""
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status

from app.core.dependencies import SettingsDep
from app.core.config import Settings
from app.schemas.fault_injection import FaultInjectionConfig
from app.services import fault_injection

router = APIRouter()


def _guard(settings: Settings, token: Optional[str]) -> None:
    enabled = settings.FAULT_INJECTION_API_ENABLED
    if enabled is None:
        enabled = settings.ENVIRONMENT != "production"
    if not enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    expected = settings.FAULT_INJECTION_TOKEN
    if expected and not (token and secrets.compare_digest(token, expected)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid X-Fault-Token")


@router.get("", response_model=FaultInjectionConfig, summary="Current failure-injection settings")
async def get_fault_injection(
    settings: SettingsDep, x_fault_token: Optional[str] = Header(None)
) -> FaultInjectionConfig:
    _guard(settings, x_fault_token)
    return fault_injection.get_config(settings)


@router.put("", response_model=FaultInjectionConfig, summary="Change failure injection at runtime")
async def put_fault_injection(
    payload: FaultInjectionConfig,
    settings: SettingsDep,
    x_fault_token: Optional[str] = Header(None),
) -> FaultInjectionConfig:
    _guard(settings, x_fault_token)
    return fault_injection.set_config(payload)


@router.delete("", response_model=FaultInjectionConfig, summary="Reset to FAILURE_RATE / LATENCY_MS from env")
async def reset_fault_injection(
    settings: SettingsDep, x_fault_token: Optional[str] = Header(None)
) -> FaultInjectionConfig:
    _guard(settings, x_fault_token)
    return fault_injection.reset(settings)
