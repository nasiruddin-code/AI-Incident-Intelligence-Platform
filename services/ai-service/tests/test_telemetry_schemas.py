from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.telemetry import (
    LogRecord,
    MetricPoint,
    MetricType,
    Resource,
    SeverityLevel,
    Span,
    SpanStatus,
)


def test_resource_schema():
    resource = Resource(service_name="test-service", environment="dev")
    assert resource.service_name == "test-service"
    
    # round-trip
    dumped = resource.model_dump_json()
    validated = Resource.model_validate_json(dumped)
    assert validated.service_name == "test-service"
    assert validated.environment == "dev"
    
    with pytest.raises(ValidationError):
        Resource()  # Missing required field


def test_log_record_schema():
    resource = Resource(service_name="test-service")
    now = datetime.now(timezone.utc)
    log = LogRecord(
        timestamp=now,
        severity=SeverityLevel.INFO,
        body="Test log",
        resource=resource
    )
    assert log.severity == SeverityLevel.INFO
    
    dumped = log.model_dump_json()
    validated = LogRecord.model_validate_json(dumped)
    assert validated.body == "Test log"
    
    with pytest.raises(ValidationError):
        LogRecord(
            timestamp=now,
            severity="INVALID_SEVERITY",  # Invalid enum
            body="Test",
            resource=resource
        )


def test_metric_point_schema():
    resource = Resource(service_name="test-service")
    now = datetime.now(timezone.utc)
    metric = MetricPoint(
        name="test_metric",
        type=MetricType.COUNTER,
        value=1.0,
        timestamp=now,
        resource=resource
    )
    assert metric.type == MetricType.COUNTER
    
    dumped = metric.model_dump_json()
    validated = MetricPoint.model_validate_json(dumped)
    assert validated.value == 1.0


def test_span_schema():
    resource = Resource(service_name="test-service")
    now = datetime.now(timezone.utc)
    span = Span(
        trace_id="trace1",
        span_id="span1",
        name="test_span",
        start_time=now,
        end_time=now,
        status=SpanStatus.OK,
        resource=resource
    )
    assert span.status == SpanStatus.OK
    
    dumped = span.model_dump_json()
    validated = Span.model_validate_json(dumped)
    assert validated.trace_id == "trace1"
