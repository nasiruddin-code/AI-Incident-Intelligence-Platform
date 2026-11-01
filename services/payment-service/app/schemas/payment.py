from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class PaymentStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"


class PaymentCreate(BaseModel):
    order_id: str = Field(..., description="ID of the order being paid")
    amount: float = Field(..., gt=0, description="Payment amount")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    customer_id: Optional[str] = None
    payment_method: str = Field(default="card", description="card | wallet | bank")


class PaymentResponse(BaseModel):
    payment_id: str
    order_id: str
    amount: float
    currency: str
    status: PaymentStatus
    customer_id: Optional[str] = None
    payment_method: str
    failure_reason: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None
