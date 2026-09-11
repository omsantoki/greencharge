"""Pydantic v2 request/response models for the HTTP API.

Datetimes are timezone-aware UTC and serialise as ISO-8601 with an offset.
"""
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


# Phase 2 debug endpoints. Both bodies are parsed with routers.parse_json_body, so they work
# without a Content-Type header. Float fields reject NaN and +/-Infinity (the JSON parser accepts
# `Infinity` and overflowing literals such as 1e400, and inf would pass a plain `gt=0`).


class PlugInRequest(BaseModel):
    """POST /api/debug/plug-in: simulate a car being plugged into a charger."""

    charger_id: int
    vehicle_model: str  # must name a vehicle in data/vehicles.json
    soc_start: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    soc_target: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    hours_until_departure: float = Field(gt=0.0, allow_inf_nan=False)  # simulated hours

    @model_validator(mode="after")
    def _target_above_start(self) -> Self:
        if self.soc_target <= self.soc_start:
            raise ValueError(
                f"soc_target ({self.soc_target}) must be greater than soc_start ({self.soc_start})"
            )
        return self


class SetLimitRequest(BaseModel):
    """POST /api/debug/set-limit: push a manual power limit to the charger's active session."""

    charger_id: int
    limit_w: float = Field(ge=0.0, allow_inf_nan=False)  # watts
