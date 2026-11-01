"""Alertmanager webhook receiver (Week 16+)."""
from fastapi import APIRouter, BackgroundTasks
from starlette.concurrency import run_in_threadpool

from app.core.dependencies import AIProviderDep, SettingsDep
from app.schemas.alerts import AlertIngestResult, AlertmanagerWebhook
from app.services.alert_ingest import ingest_and_plan, investigate

router = APIRouter()


@router.post(
    "/alertmanager",
    response_model=AlertIngestResult,
    summary="Alertmanager webhook: open/update/resolve an incident and investigate it",
)
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    background: BackgroundTasks,
    provider: AIProviderDep,
    settings: SettingsDep,
) -> AlertIngestResult:
    """Point an Alertmanager `webhook_configs` receiver here (send_resolved: true).

    Responds immediately; enrichment, correlation, RCA and runbook generation
    run in the background and are recorded on the incident
    (`metadata.auto_investigation`).
    """
    result, investigate_id = await run_in_threadpool(ingest_and_plan, payload, settings)
    if investigate_id:
        background.add_task(investigate, investigate_id, provider, settings)
    return result
