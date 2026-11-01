from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"
    CANCELLED = "cancelled"


class OrderItem(BaseModel):
    sku: str
    name: str
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., gt=0)


class OrderCreate(BaseModel):
    customer_id: str
    items: list[OrderItem] = Field(..., min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    payment_method: str = Field(default="card")


class OrderResponse(BaseModel):
    order_id: str
    customer_id: str
    items: list[OrderItem]
    total_amount: float
    currency: str
    status: OrderStatus
    payment_id: Optional[str] = None
    payment_status: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
