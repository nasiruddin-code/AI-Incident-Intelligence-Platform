"""Runtime failure-injection state (Week 8 incident scenarios).

Starts from FAILURE_RATE / LATENCY_MS in settings and can be changed at
runtime via /api/v1/fault-injection, so scenarios no longer need a restart.
State is per process (the sample service runs a single replica).
"""
from __future__ import annotations

import threading
from typing import Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.fault_injection import FaultInjectionConfig

logger = get_logger(__name__)

_lock = threading.Lock()
_config: Optional[FaultInjectionConfig] = None


def _from_settings(settings: Settings) -> FaultInjectionConfig:
    return FaultInjectionConfig(failure_rate=settings.FAILURE_RATE, latency_ms=settings.LATENCY_MS)


def get_config(settings: Settings) -> FaultInjectionConfig:
    global _config
    with _lock:
        if _config is None:
            _config = _from_settings(settings)
        return _config


def set_config(config: FaultInjectionConfig) -> FaultInjectionConfig:
    global _config
    with _lock:
        previous, _config = _config, config
    logger.warning(
        "Fault injection changed: failure_rate=%s latency_ms=%s (was %s)",
        config.failure_rate,
        config.latency_ms,
        previous.model_dump() if previous else "settings default",
    )
    return config


def reset(settings: Settings) -> FaultInjectionConfig:
    return set_config(_from_settings(settings))
