from fastapi import APIRouter

from app.schemas.telemetry import LogRecord, MetricPoint, Span

router = APIRouter()


@router.post("/logs", response_model=LogRecord, summary="Ingest a log record")
async def ingest_log(log: LogRecord) -> LogRecord:
    """
    Accepts a LogRecord and echoes it back.
    In future weeks, this will forward to the OTel Collector or ingestion pipeline.
    """
    return log


@router.post("/metrics", response_model=MetricPoint, summary="Ingest a metric point")
async def ingest_metric(metric: MetricPoint) -> MetricPoint:
    """
    Accepts a MetricPoint and echoes it back.
    """
    return metric


@router.post("/traces", response_model=Span, summary="Ingest a trace span")
async def ingest_trace(span: Span) -> Span:
    """
    Accepts a Span and echoes it back.
    """
    return span
