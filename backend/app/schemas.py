"""Pydantic v2 request/response models for the HTTP API.

Datetimes are timezone-aware UTC and serialise as ISO-8601 with an offset.
"""
from datetime import datetime, timezone
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


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


# Phase 4 orchestration endpoints. Their response models fix the exact shapes of the
# implementation contract (6b), so a response never carries a key the contract does not name.


def _as_utc(value: datetime) -> datetime:
    """The same instant in UTC. A naive datetime is rejected: its instant is unknown."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


# A datetime served as UTC, whatever offset the database or the orchestrator handed over.
UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class ScheduleSlotOut(BaseModel):
    """One 15-minute slot of a session's planned charging power."""

    slot_start: UtcDatetime
    power_kw: float


class ActiveSessionOut(BaseModel):
    """GET /api/sessions/active: every Session column plus the session's orchestration state."""

    # models.Session columns
    id: int
    charger_id: int
    ocpp_transaction_id: int | None
    vehicle_model: str
    battery_kwh: float
    max_charge_kw: float
    soc_start: float
    soc_target: float
    soc_current: float
    plugged_in_at: UtcDatetime
    deadline: UtcDatetime
    energy_delivered_kwh: float
    co2_actual_g: float
    co2_baseline_g: float
    cost_actual_inr: float
    cost_baseline_inr: float
    status: str
    # orchestration state
    ocpp_id: str
    manual_limit_w: float | None  # operator limit (override or debug set-limit), W
    projected_unmet_kwh: float | None  # the last tick's unmet energy; None if it had none
    on_time: bool  # the last tick left no unmet energy for this session
    schedule: list[ScheduleSlotOut]  # the latest plan, 96 slots; [] before the first plan


class SessionScheduleOut(BaseModel):
    """GET /api/sessions/{id}/schedule: the session's latest plan."""

    session_id: int
    computed_at: UtcDatetime
    slots: list[ScheduleSlotOut]


class LoadCurveSlotOut(BaseModel):
    slot_start: UtcDatetime
    optimized_kw: float  # measured (is_past) or planned aggregate site power
    baseline_kw: float  # the naive shadow simulation's aggregate site power
    is_past: bool


class LoadCurveOut(BaseModel):
    """GET /api/sites/{id}/load-curve: optimized vs baseline aggregate kW, 96 slots."""

    site_id: int
    max_power_kw: float
    window_start: UtcDatetime
    now: UtcDatetime
    slots: list[LoadCurveSlotOut]
    optimized_peak_kw: float
    baseline_peak_kw: float


class ImpactSummaryOut(BaseModel):
    """GET /api/impact/summary: exactly the four keys of the build spec."""

    co2_saved_kg: float
    cost_saved_inr: float
    sessions_on_time: int
    total_sessions: int


class WeightsRequest(BaseModel):
    """POST /api/optimizer/weights: the objective weights (alpha: carbon, beta: cost).

    Non-finite values are refused: the optimizer rejects them.
    """

    alpha: float = Field(ge=0.0, allow_inf_nan=False)
    beta: float = Field(ge=0.0, allow_inf_nan=False)


class OverrideRequest(BaseModel):
    """POST /api/sessions/{id}/override takes no parameters (the session is in the path).

    The body may be empty or a JSON object; any keys in it are ignored.
    """
