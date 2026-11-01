from pydantic import BaseModel, Field


class FaultInjectionConfig(BaseModel):
    failure_rate: float = Field(0.0, ge=0.0, le=1.0, description="Probability a payment fails")
    latency_ms: int = Field(0, ge=0, le=30000, description="Artificial delay per payment")
