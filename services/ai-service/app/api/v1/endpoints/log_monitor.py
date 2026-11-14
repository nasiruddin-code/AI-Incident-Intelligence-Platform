"""Log monitor endpoints: status, and a manual poll (demos / tests)."""
from typing import Any, Dict

from fastapi import APIRouter

from app.services.log_monitor import get_monitor

router = APIRouter()


@router.get("/status", summary="Log monitor status: cursor, counters, top error patterns")
async def log_monitor_status() -> Dict[str, Any]:
    return get_monitor().status()


@router.post("/run", summary="Run one log-monitor poll now")
async def log_monitor_run(wait: bool = False) -> Dict[str, Any]:
    """Reads new log lines from Loki, detects error patterns and opens or updates
    incidents. Works whether or not the background loop is enabled. With
    `wait=true` the response waits for notifications and investigations started
    by this poll to finish (slow with a real AI model)."""
    monitor = get_monitor()
    summary = await monitor.run_once()
    if wait:
        await monitor.wait_idle()
    return {"summary": summary, "status": monitor.status()}
