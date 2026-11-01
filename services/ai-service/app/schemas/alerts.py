"""Alertmanager webhook payload (version 4) and ingestion result."""
from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["firing", "resolved"]
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[datetime] = None
    endsAt: Optional[datetime] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertmanagerWebhook(BaseModel):
    """One notification = one alert group (see Alertmanager's group_by)."""
    model_config = ConfigDict(extra="ignore")

    version: str = "4"
    groupKey: str
    status: Literal["firing", "resolved"]
    receiver: str = ""
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: List[AlertmanagerAlert] = Field(default_factory=list)


class AlertIngestResult(BaseModel):
    incident_id: Optional[str] = None
    action: Literal["created", "updated", "resolved", "ignored"]
    alertnames: List[str] = Field(default_factory=list)
    new_alerts: int = 0
    investigation_scheduled: bool = False
    detail: str = ""
