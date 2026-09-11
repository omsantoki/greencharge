"""Pydantic v2 request/response models for the HTTP API.

Datetimes are timezone-aware UTC and serialise as ISO-8601 with an offset.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CarbonPointOut(BaseModel):
    """One grid carbon-intensity point as served by /api/grid/*.

    ``source`` is the provider that produced it: "estimated" for the synthetic fallback
    (never measured) or "electricitymaps".
    """

    ts: datetime
    carbon_intensity: float  # gCO2eq/kWh
    renewable_pct: float | None
    fossil_pct: float | None
    source: str


class ChargerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    ocpp_id: str
    max_power_kw: float
    status: str
    last_heartbeat: datetime | None


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    latitude: float
    longitude: float
    grid_zone: str
    max_power_kw: float
    demand_charge_inr_per_kva: float
    chargers: list[ChargerOut]
