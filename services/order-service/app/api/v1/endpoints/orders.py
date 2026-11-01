from fastapi import APIRouter, HTTPException, status

from app.core.dependencies import SettingsDep
from app.core.logging import get_logger
from app.schemas.order import OrderCreate, OrderResponse
from app.services.order_manager import OrderManager

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an order (calls payment-service)",
)
async def create_order(payload: OrderCreate, settings: SettingsDep) -> OrderResponse:
    manager = OrderManager(settings)
    return await manager.create(payload)


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    summary="Get order by ID",
)
async def get_order(order_id: str, settings: SettingsDep) -> OrderResponse:
    manager = OrderManager(settings)
    order = manager.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return order


@router.get(
    "",
    response_model=list[OrderResponse],
    summary="List all orders",
)
async def list_orders(settings: SettingsDep) -> list[OrderResponse]:
    manager = OrderManager(settings)
    return manager.list_all()
