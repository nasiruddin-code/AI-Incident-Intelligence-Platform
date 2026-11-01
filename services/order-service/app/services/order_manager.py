"""Order management + outbound call to payment-service."""
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import get_meter, get_tracer
from app.schemas.order import OrderCreate, OrderResponse, OrderStatus

logger = get_logger(__name__)

_orders: Dict[str, OrderResponse] = {}
_orders_total = None
_order_duration = None


def _metrics():
    global _orders_total, _order_duration
    if _orders_total is None:
        meter = get_meter("order-service")
        _orders_total = meter.create_counter(
            name="orders_total",
            description="Total number of orders created",
            unit="1",
        )
        _order_duration = meter.create_histogram(
            name="order_duration_seconds",
            description="End-to-end order creation duration (includes payment call)",
            unit="s",
            # Seconds-scale buckets: the SDK defaults (0, 5, 10, ... 10000) assume
            # milliseconds, which put every request in the first bucket and made
            # histogram_quantile report ~4.75s for millisecond latencies.
            explicit_bucket_boundaries_advisory=[
                0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0
            ],
        )
    return _orders_total, _order_duration


class OrderManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.payment_base = settings.PAYMENT_SERVICE_URL.rstrip("/")

    def _total(self, payload: OrderCreate) -> float:
        return round(sum(i.quantity * i.unit_price for i in payload.items), 2)

    async def create(self, payload: OrderCreate) -> OrderResponse:
        tracer = get_tracer("order-service")
        with tracer.start_as_current_span(
            "order.create",
            attributes={
                "order.customer_id": payload.customer_id,
                "order.item_count": len(payload.items),
                "order.currency": payload.currency,
            },
        ) as span:
            start = time.perf_counter()
            order_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            total = self._total(payload)

            order = OrderResponse(
                order_id=order_id,
                customer_id=payload.customer_id,
                items=payload.items,
                total_amount=total,
                currency=payload.currency,
                status=OrderStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            _orders[order_id] = order
            span.set_attribute("order.id", order_id)
            span.set_attribute("order.total_amount", total)
            logger.info(
                "Order created order_id=%s customer_id=%s total=%.2f",
                order_id, payload.customer_id, total,
            )

            payment_payload = {
                "order_id": order_id,
                "amount": total,
                "currency": payload.currency,
                "customer_id": payload.customer_id,
                "payment_method": payload.payment_method,
            }

            with tracer.start_as_current_span(
                "order.call_payment_service",
                attributes={"http.method": "POST", "peer.service": "payment-service"},
            ) as payment_span:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            f"{self.payment_base}/api/v1/payments",
                            json=payment_payload,
                        )
                        resp.raise_for_status()
                        payment = resp.json()
                        payment_span.set_attribute("http.status_code", resp.status_code)
                except httpx.HTTPError as exc:
                    logger.error(
                        "Payment service call failed order_id=%s error=%s",
                        order_id, exc,
                    )
                    payment_span.record_exception(exc)
                    payment_span.set_attribute("error", True)
                    order.status = OrderStatus.PAYMENT_FAILED
                    order.failure_reason = f"Payment service unreachable: {exc}"
                    order.updated_at = datetime.now(timezone.utc)
                    _orders[order_id] = order
                    counter, hist = _metrics()
                    counter.add(1, {"status": "payment_failed"})
                    hist.record(time.perf_counter() - start, {"status": "payment_failed"})
                    span.set_attribute("order.status", "payment_failed")
                    return order

            order.payment_id = payment.get("payment_id")
            order.payment_status = payment.get("status")
            order.updated_at = datetime.now(timezone.utc)

            if payment.get("status") == "completed":
                order.status = OrderStatus.PAID
                logger.info(
                    "Order PAID order_id=%s payment_id=%s",
                    order_id, order.payment_id,
                )
            else:
                order.status = OrderStatus.PAYMENT_FAILED
                order.failure_reason = payment.get("failure_reason") or "Payment declined"
                logger.warning(
                    "Order PAYMENT_FAILED order_id=%s reason=%s",
                    order_id, order.failure_reason,
                )

            _orders[order_id] = order
            status_label = order.status.value
            counter, hist = _metrics()
            counter.add(1, {"status": status_label})
            hist.record(time.perf_counter() - start, {"status": status_label})
            span.set_attribute("order.status", status_label)
            if order.payment_id:
                span.set_attribute("order.payment_id", order.payment_id)
            return order

    def get(self, order_id: str) -> Optional[OrderResponse]:
        return _orders.get(order_id)

    def list_all(self) -> list[OrderResponse]:
        return list(_orders.values())
