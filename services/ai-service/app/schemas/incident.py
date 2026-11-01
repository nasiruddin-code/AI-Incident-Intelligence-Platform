from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.telemetry import LogRecord, MetricPoint, SeverityLevel, Span


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class ScenarioType(str, Enum):
    """Predefined failure scenarios for reproducible incidents (Week 8)."""
    PAYMENT_TIMEOUT = "payment_timeout"
    PAYMENT_HIGH_LATENCY = "payment_high_latency"
    PAYMENT_CASCADE_FAILURE = "payment_cascade_failure"
    MANUAL = "manual"


class TimeWindow(BaseModel):
    start: datetime = Field(..., description="Start time of the window")
    end: datetime = Field(..., description="End time of the window")


class IncidentContext(BaseModel):
    """Normalized incident package used by correlation, RCA, and the agent."""
    incident_id: str = Field(..., description="Unique identifier for the incident")
    title: str = Field(..., description="Short descriptive title for the incident")
    status: IncidentStatus = Field(..., description="Current status of the incident")
    severity: SeverityLevel = Field(..., description="Severity level of the incident")
    affected_services: List[str] = Field(..., description="List of services affected by the incident")
    time_window: TimeWindow = Field(..., description="Time window when the incident occurred")
    scenario: Optional[ScenarioType] = Field(None, description="Scenario that produced this incident, if any")
    logs: List[LogRecord] = Field(default_factory=list, description="Logs associated with the incident")
    metrics: List[MetricPoint] = Field(default_factory=list, description="Metrics associated with the incident")
    traces: List[Span] = Field(default_factory=list, description="Traces associated with the incident")
    evidence_summary: Optional[str] = Field(
        None, description="Short human-readable summary of collected evidence"
    )
    notes: Optional[str] = Field(None, description="Free text notes or AI summary regarding the incident")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Extra structured metadata")


# --- API request / response models ----------------------------------------

class IncidentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    severity: SeverityLevel = SeverityLevel.ERROR
    affected_services: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    scenario: Optional[ScenarioType] = None


class ScenarioRunRequest(BaseModel):
    """Trigger a reproducible failure scenario against the sample microservices."""
    scenario: ScenarioType = Field(..., description="Which failure scenario to run")
    order_count: int = Field(default=5, ge=1, le=50, description="How many orders to fire")
    # Override targets (defaults point at docker-compose service names)
    order_service_url: str = Field(
        default="http://order-service:8001",
        description="Base URL of order-service",
    )
    payment_service_url: str = Field(
        default="http://payment-service:8002",
        description="Base URL of payment-service (its /fault-injection API is used to apply the scenario)",
    )
    apply_failure_config: bool = Field(
        default=True,
        description="Set payment-service FAILURE_RATE/LATENCY_MS for this scenario before firing orders",
    )
    restore_failure_config: bool = Field(
        default=True,
        description="Put payment-service back to its previous failure settings afterwards",
    )


class ScenarioRunResult(BaseModel):
    scenario: ScenarioType
    incident_id: str
    orders_attempted: int
    orders_paid: int
    orders_failed: int
    message: str
    incident: IncidentContext


class BuildContextRequest(BaseModel):
    """Build / enrich an IncidentContext from provided telemetry evidence."""
    incident_id: Optional[str] = Field(None, description="Existing incident to enrich; omit to create new")
    title: Optional[str] = None
    severity: SeverityLevel = SeverityLevel.ERROR
    affected_services: List[str] = Field(default_factory=list)
    time_window: Optional[TimeWindow] = None
    logs: List[LogRecord] = Field(default_factory=list)
    metrics: List[MetricPoint] = Field(default_factory=list)
    traces: List[Span] = Field(default_factory=list)
    notes: Optional[str] = None
    scenario: Optional[ScenarioType] = None


class IncidentListResponse(BaseModel):
    count: int = Field(..., description="Number of incidents in this page")
    total: int = Field(0, description="Total incidents matching the filters")
    limit: int = 50
    offset: int = 0
    incidents: List[IncidentContext]


class IncidentSummary(BaseModel):
    """Incident without logs/metrics/traces — cheap to list."""
    incident_id: str
    title: str
    status: IncidentStatus
    severity: SeverityLevel
    affected_services: List[str] = Field(default_factory=list)
    scenario: Optional[ScenarioType] = None
    evidence_summary: Optional[str] = None


class EnrichRequest(BaseModel):
    """Pull live evidence from Prometheus / Loki / Tempo into an incident."""
    lookback_seconds: Optional[int] = Field(
        None, ge=60, le=86400,
        description="Window size ending at the incident end; default ENRICH_LOOKBACK_SECONDS",
    )
    services: Optional[List[str]] = Field(
        None, description="Services to query; defaults to the incident's affected_services"
    )


class EnrichmentSourceStatus(BaseModel):
    ok: bool
    fetched: int = 0
    added: int = 0
    error: Optional[str] = None


class EnrichmentResult(BaseModel):
    incident_id: str
    window: TimeWindow
    services: List[str]
    sources: Dict[str, EnrichmentSourceStatus]
    logs_added: int
    metrics_added: int
    spans_added: int
    evidence_summary: Optional[str] = None


class IncidentSummaryListResponse(BaseModel):
    count: int = Field(..., description="Number of incidents in this page")
    total: int = Field(..., description="Total incidents matching the filters")
    limit: int
    offset: int
    incidents: List[IncidentSummary]


# --- Week 9: Correlation & Anomaly Detection --------------------------------

class CorrelationLink(BaseModel):
    """A link between two pieces of evidence (e.g. log ↔ span via trace_id)."""
    link_type: str = Field(..., description="trace_id | service | time_proximity | metric_spike")
    source_kind: str = Field(..., description="log | metric | span")
    source_ref: str = Field(..., description="Identifier or short description of source")
    target_kind: str = Field(..., description="log | metric | span")
    target_ref: str = Field(..., description="Identifier or short description of target")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Link confidence 0–1")
    detail: Optional[str] = None


class AnomalyFinding(BaseModel):
    """A detected anomaly in the incident evidence."""
    anomaly_type: str = Field(
        ...,
        description="error_rate_spike | latency_spike | error_log_burst | error_span_burst | service_fanout",
    )
    service: Optional[str] = None
    description: str
    score: float = Field(..., ge=0.0, le=1.0, description="Anomaly strength 0–1")
    related_metric: Optional[str] = None
    evidence_count: int = 0


class SeverityScore(BaseModel):
    """Computed severity with breakdown."""
    level: SeverityLevel
    score: float = Field(..., ge=0.0, le=100.0, description="Numeric severity 0–100")
    factors: Dict[str, float] = Field(
        default_factory=dict,
        description="Named factors that contributed to the score",
    )
    rationale: str


class CorrelationResult(BaseModel):
    """Full correlation + anomaly + severity output for an incident."""
    incident_id: str
    links: List[CorrelationLink] = Field(default_factory=list)
    anomalies: List[AnomalyFinding] = Field(default_factory=list)
    severity: SeverityScore
    correlated_trace_ids: List[str] = Field(default_factory=list)
    service_graph: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="service -> list of peer services seen in evidence",
    )
    summary: str
    incident: IncidentContext
