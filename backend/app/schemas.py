from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class BookingCreate(BaseModel):
    patient_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=20)
    pickup_lat: float = Field(ge=-90, le=90)
    pickup_lon: float = Field(ge=-180, le=180)
    hospital_id: str
    notes: Optional[str] = Field(default=None, max_length=500)
    auto_assign: bool = True

    @field_validator("phone")
    @classmethod
    def phone_digits(cls, v: str) -> str:
        cleaned = v.replace(" ", "").replace("-", "")
        if not cleaned.lstrip("+").isdigit():
            raise ValueError("phone must contain digits only (optionally starting with +)")
        return cleaned


class AssignRequest(BaseModel):
    ambulance_id: Optional[str] = None


class BookingOut(BaseModel):
    id: int
    status: str
    patient_name: str
    pickup_lat: float
    pickup_lon: float
    pickup_node: Optional[str]
    hospital_id: str
    hospital_name: Optional[str]
    ambulance_id: Optional[str]
    eta_s: Optional[float]
    failure_reason: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
    run_id: int
    live: Optional[dict[str, Any]] = None


class SimulationControl(BaseModel):
    action: Optional[str] = Field(default=None, description="pause | resume | reset")
    speed: Optional[float] = Field(default=None, gt=0, le=50)
    demand: Optional[str] = None
    priority_enabled: Optional[bool] = None
    recovery_enabled: Optional[bool] = None
    queue_source: Optional[str] = None
    seed: Optional[int] = None
    engine: Optional[str] = None
    auto_emergencies_per_hour: Optional[float] = Field(default=None, ge=0, le=600)


class ManualPriority(BaseModel):
    approach: str
    action: str = Field(default="request", description="request | release")
    hold_s: Optional[float] = Field(default=None, gt=0, le=300, description="auto-release after this many seconds")


class TestAmbulance(BaseModel):
    approach: Optional[str] = None
    upstream_m: float = Field(default=800, ge=150, le=3000)


class ExperimentRequest(BaseModel):
    demands: list[str] = ["low", "medium", "high"]
    seeds: list[int] = [1, 2, 3]
    engine: str = "builtin"
    warmup_s: float = Field(default=300, ge=0, le=3600)
    recovery_tail_s: float = Field(default=240, ge=0, le=1800)
    base_id: Optional[str] = None
    pickup_node: Optional[str] = None
    hospital_id: Optional[str] = None
