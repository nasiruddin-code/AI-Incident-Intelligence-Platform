from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.incident import IncidentContext, IncidentStatus, TimeWindow
from app.schemas.telemetry import SeverityLevel


def test_incident_context_schema():
    now = datetime.now(timezone.utc)
    window = TimeWindow(start=now, end=now)
    incident = IncidentContext(
        incident_id="inc-123",
        title="DB Outage",
        status=IncidentStatus.OPEN,
        severity=SeverityLevel.ERROR,
        affected_services=["db-service"],
        time_window=window
    )
    assert incident.status == IncidentStatus.OPEN
    assert incident.severity == SeverityLevel.ERROR
    
    # round-trip
    dumped = incident.model_dump_json()
    validated = IncidentContext.model_validate_json(dumped)
    assert validated.incident_id == "inc-123"
    assert validated.time_window.start == now
    
    # invalid case
    with pytest.raises(ValidationError):
        IncidentContext(
            incident_id="inc-123",
            title="DB Outage",
            status="INVALID_STATUS", # Invalid enum
            severity=SeverityLevel.ERROR,
            affected_services=["db-service"],
            time_window=window
        )
