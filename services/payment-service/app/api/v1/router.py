from fastapi import APIRouter

from app.api.v1.endpoints import fault_injection, health, payments

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])
api_router.include_router(fault_injection.router, prefix="/fault-injection", tags=["fault-injection"])
