"""Payment processing business logic (in-memory for sample microservice)."""
import asyncio
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import get_meter, get_tracer
from app.schemas.payment import PaymentCreate, PaymentResponse, PaymentStatus

logger = get_logger(__name__)

_payments: Dict[str, PaymentResponse] = {}
_payments_total = None
_payment_duration = None


def _metrics():
    global _payments_total, _payment_duration
    if _payments_total is None:
        meter = get_meter("payment-service")
        _payments_total = meter.create_counter(
            name="payments_total",
            description="Total number of payment attempts",
            unit="1",
        )
        _payment_duration = meter.create_histogram(
            name="payment_duration_seconds",
            description="Payment processing duration",
            unit="s",
        )
    return _payments_total, _payment_duration


class PaymentProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def process(self, payload: PaymentCreate) -> PaymentResponse:
        tracer = get_tracer("payment-service")
        with tracer.start_as_current_span(
            "payment.process",
            attributes={
                "payment.order_id": payload.order_id,
                "payment.amount": payload.amount,
                "payment.currency": payload.currency,
                "payment.method": payload.payment_method,
            },
        ) as span:
            start = time.perf_counter()
            payment_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)

            if self.settings.LATENCY_MS > 0:
                await asyncio.sleep(self.settings.LATENCY_MS / 1000.0)

            if random.random() < self.settings.FAILURE_RATE:
                status = PaymentStatus.FAILED
                failure_reason = "Simulated payment gateway timeout"
                logger.warning(
                    "Payment FAILED (injected) payment_id=%s order_id=%s amount=%.2f",
                    payment_id, payload.order_id, payload.amount,
                )
                span.set_attribute("payment.status", "failed")
                span.set_attribute("payment.failure_reason", failure_reason)
            else:
                status = PaymentStatus.COMPLETED
                failure_reason = None
                logger.info(
                    "Payment COMPLETED payment_id=%s order_id=%s amount=%.2f currency=%s",
                    payment_id, payload.order_id, payload.amount, payload.currency,
                )
                span.set_attribute("payment.status", "completed")

            duration = time.perf_counter() - start
            counter, hist = _metrics()
            counter.add(1, {"status": status.value, "currency": payload.currency})
            hist.record(duration, {"status": status.value})

            payment = PaymentResponse(
                payment_id=payment_id,
                order_id=payload.order_id,
                amount=payload.amount,
                currency=payload.currency,
                status=status,
                customer_id=payload.customer_id,
                payment_method=payload.payment_method,
                failure_reason=failure_reason,
                created_at=now,
                processed_at=now if status == PaymentStatus.COMPLETED else None,
            )
            _payments[payment_id] = payment
            span.set_attribute("payment.id", payment_id)
            return payment

    def get(self, payment_id: str) -> Optional[PaymentResponse]:
        return _payments.get(payment_id)

    def list_by_order(self, order_id: str) -> list[PaymentResponse]:
        return [p for p in _payments.values() if p.order_id == order_id]
