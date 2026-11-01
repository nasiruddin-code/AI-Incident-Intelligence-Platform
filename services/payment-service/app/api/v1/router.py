from fastapi import APIRouter

from app.api.v1.endpoints import health, payments

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])
