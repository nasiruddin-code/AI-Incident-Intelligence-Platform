from fastapi import APIRouter, HTTPException, status

from app.core.dependencies import SettingsDep
from app.core.logging import get_logger
from app.schemas.payment import PaymentCreate, PaymentResponse
from app.services.payment_processor import PaymentProcessor

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Process a payment",
)
async def create_payment(payload: PaymentCreate, settings: SettingsDep) -> PaymentResponse:
    processor = PaymentProcessor(settings)
    payment = await processor.process(payload)
    if payment.status.value == "failed":
        # Still return 201 with failed status so callers can inspect the body.
        # (Real systems often return 402/502 — we keep it simple for the sample.)
        logger.warning("Returning failed payment %s", payment.payment_id)
    return payment


@router.get(
    "/{payment_id}",
    response_model=PaymentResponse,
    summary="Get payment by ID",
)
async def get_payment(payment_id: str, settings: SettingsDep) -> PaymentResponse:
    processor = PaymentProcessor(settings)
    payment = processor.get(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")
    return payment


@router.get(
    "/by-order/{order_id}",
    response_model=list[PaymentResponse],
    summary="List payments for an order",
)
async def list_payments_for_order(order_id: str, settings: SettingsDep) -> list[PaymentResponse]:
    processor = PaymentProcessor(settings)
    return processor.list_by_order(order_id)
