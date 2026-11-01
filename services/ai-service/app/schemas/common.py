from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_name: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    ai_provider: str
    database: str = "memory"  # memory | postgres | error
