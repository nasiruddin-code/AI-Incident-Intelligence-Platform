from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class Resource(BaseModel):
    service_name: str = Field(..., description="Name of the service that generated the telemetry")
    service_version: Optional[str] = Field(None, description="Version of the service")
    environment: Optional[str] = Field(None, description="Deployment environment (e.g., production, staging)")
    attributes: Dict[str, str] = Field(default_factory=dict, description="Additional resource attributes")


class SeverityLevel(str, Enum):
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


class LogRecord(BaseModel):
    timestamp: datetime = Field(..., description="Time when the log was generated")
    severity: SeverityLevel = Field(..., description="Severity level of the log")
    body: str = Field(..., description="Log message body")
    attributes: Dict[str, Any] = Field(default_factory=dict, description="Additional log attributes")
    resource: Resource = Field(..., description="Resource that generated the log")
    trace_id: Optional[str] = Field(None, description="Associated trace ID, if any")
    span_id: Optional[str] = Field(None, description="Associated span ID, if any")


class MetricType(str, Enum):
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    HISTOGRAM = "HISTOGRAM"


class MetricPoint(BaseModel):
    name: str = Field(..., description="Name of the metric")
    description: Optional[str] = Field(None, description="Description of the metric")
    unit: Optional[str] = Field(None, description="Unit of measure for the metric")
    type: MetricType = Field(..., description="Type of the metric")
    value: float = Field(..., description="Metric value")
    timestamp: datetime = Field(..., description="Time when the metric was recorded")
    attributes: Dict[str, str] = Field(default_factory=dict, description="Additional metric attributes")
    resource: Resource = Field(..., description="Resource that generated the metric")


class SpanStatus(str, Enum):
    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


class Span(BaseModel):
    trace_id: str = Field(..., description="ID of the trace this span belongs to")
    span_id: str = Field(..., description="ID of this specific span")
    parent_span_id: Optional[str] = Field(None, description="ID of the parent span, if any")
    name: str = Field(..., description="Name of the span operation")
    start_time: datetime = Field(..., description="Start time of the span")
    end_time: datetime = Field(..., description="End time of the span")
    status: SpanStatus = Field(..., description="Status of the span execution")
    attributes: Dict[str, Any] = Field(default_factory=dict, description="Additional span attributes")
    resource: Resource = Field(..., description="Resource that generated the span")
