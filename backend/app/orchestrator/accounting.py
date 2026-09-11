"""CO2 and cost attribution (BUILD_SPEC Phase 4, "Accounting") and the impact summary.

Per meter-value interval, as the spec writes it::

    co2_actual_g    += energy_kwh_in_interval x carbon_intensity_at_that_time
    cost_actual_inr += energy_kwh_in_interval x price_at_that_time

using the ACTUAL carbon intensity at that timestamp, never the forecast ("Honest accounting").
``actual_carbon_intensity`` supplies it and never reads forecast rows; the OCPP MeterValues and
StopTransaction handlers call ``apply_interval`` with the change of the energy register.
Energies are grid-side (what the charger draws, i.e. the Energy.Active.Import.Register).

``impact_summary`` compares every non-aborted session with its baseline
(``co2_baseline_g`` / ``cost_baseline_inr``, from ``baseline.simulate_baseline``):

- completed: saved = baseline - actual; on time when soc_current >= soc_target -
  settings.soc_tolerance (SoC arrives as a 3-decimal percent string).
- active: saved = baseline - (actual so far + the remaining plan), where the remaining plan is
  every slot of the session's latest schedule with slot_start >= floor_to_slot(now), priced at
  kW x slot length x FORECAST carbon intensity (and x tariff price). The forecast is the one the
  optimizer planned with: the forecast points the grid provider cached in ``grid_data``, resampled
  onto the slots with ``resample_to_slots``. On time when the last tick left the session no unmet
  energy.

Savings of an active session are therefore a projection: the actual part is measured, the plan
part is forecast. Only energy that is planned is counted, so an active session with no schedule
yet, or with unmet energy, shows more saving than it will end with.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.models import Charger, GridData, Session, Site
from app.orchestrator.baseline import COUNTED_STATUSES, SESSION_COMPLETED
from app.providers import get_provider
from app.providers.base import CarbonPoint, ProviderError, floor_to_slot, resample_to_slots
from app.providers.tariff import price_at

G_PER_KG = 1000.0
_HOUR = timedelta(hours=1)


async def actual_carbon_intensity(ts: datetime, zone: str) -> float:
    """The ACTUAL carbon intensity (gCO2eq/kWh) at ``ts`` in ``zone``, for accounting.

    With a provider that has ``ci_at`` (the synthetic one) that is ``provider.ci_at(ts, zone)``,
    its deterministic value for the slot containing ``ts``. Otherwise it is the provider's latest
    actual point, ``(await provider.get_latest(zone)).carbon_intensity``, which is the value at
    "now"; callers pass the receipt time of a meter reading, so ``ts`` is "now". Forecast rows are
    never read here. May raise ``ProviderError`` (e.g. Electricity Maps unreachable).
    """
    provider = get_provider()
    ci_at = getattr(provider, "ci_at", None)
    if callable(ci_at):
        return float(ci_at(ts, zone))
    point = await provider.get_latest(zone)
    return float(point.carbon_intensity)


def apply_interval(
    session: Any, delta_energy_kwh: float, carbon_intensity: float, price_inr_per_kwh: float
) -> None:
    """Attribute one meter-value interval to ``session`` (a ``models.Session``), in place.

    ``co2_actual_g += delta x CI`` and ``cost_actual_inr += delta x price``, with ``delta`` the
    grid-side kWh drawn in the interval. A delta <= 0 is ignored (a register reset or a repeated
    reading). The caller commits. Raises ``ValueError`` for a non-finite argument, so a bad value
    can never poison the running totals.
    """
    for name, value in (
        ("delta_energy_kwh", delta_energy_kwh),
        ("carbon_intensity", carbon_intensity),
        ("price_inr_per_kwh", price_inr_per_kwh),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {value!r}")
    if delta_energy_kwh <= 0:
        return
    session.co2_actual_g = (session.co2_actual_g or 0.0) + delta_energy_kwh * carbon_intensity
    session.cost_actual_inr = (
        session.cost_actual_inr or 0.0
    ) + delta_energy_kwh * price_inr_per_kwh


def _utc(dt: datetime, name: str) -> datetime:
    """``dt`` as aware UTC (raises ``ValueError`` for a naive value)."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")
    return dt.astimezone(timezone.utc)


def _forecast_ci(db: DbSession, zone: str, slot_starts: set[datetime]) -> dict[datetime, float]:
    """Forecast carbon intensity (gCO2eq/kWh) at each of ``slot_starts`` (UTC) in ``zone``.

    Reads the cached forecast points (``grid_data`` rows with is_forecast = true) from the last one
    at or before the earliest slot to the first one at or after the latest slot, and evaluates them
    at every slot start with ``resample_to_slots`` (linear interpolation, clamped beyond the data),
    the same way the tick builds the optimizer's carbon curve. Raises ``ProviderError`` when no
    forecast is cached for the zone at all.
    """
    first, last = min(slot_starts), max(slot_starts)
    forecast_rows = (GridData.zone == zone, GridData.is_forecast.is_(True))
    lower = db.scalar(select(func.max(GridData.ts)).where(*forecast_rows, GridData.ts <= first))
    upper = db.scalar(select(func.min(GridData.ts)).where(*forecast_rows, GridData.ts >= last))
    lo = first if lower is None else _utc(lower, "grid_data ts")
    hi = last if upper is None else _utc(upper, "grid_data ts")
    rows = db.execute(
        select(GridData.ts, GridData.carbon_intensity)
        .where(*forecast_rows, GridData.ts >= lo, GridData.ts <= hi)
        .order_by(GridData.ts)
    ).all()
    if not rows:
        raise ProviderError(
            f"No carbon-intensity forecast is cached for zone {zone}, so the remaining planned "
            f"charging of active sessions cannot be accounted for."
        )
    points = [
        CarbonPoint(ts=_utc(row.ts, "grid_data ts"), carbon_intensity=row.carbon_intensity)
        for row in rows
    ]
    # resample_to_slots is pointwise, so one slot at a time gives the tick's values exactly.
    ci: dict[datetime, float] = {}
    for slot_start in slot_starts:
        (point,) = resample_to_slots(
            points, slot_start, n_slots=1, slot_minutes=settings.slot_minutes
        )
        ci[slot_start] = float(point.carbon_intensity)
    return ci


def impact_summary(
    db: DbSession,
    now: datetime,
    latest_schedules: dict[int, dict],
    last_unmet_kwh: dict[int, float],
) -> dict:
    """Savings over every non-aborted session (see the module docstring for the rules).

    ``latest_schedules``: session id -> ``{"computed_at": dt, "slots": [(slot_start, kW)] x 96}``
    (the loop's latest plans). ``last_unmet_kwh``: session id -> unmet kWh of the last tick.
    Reads through ``db`` only. Returns EXACTLY ``{"co2_saved_kg", "cost_saved_inr",
    "sessions_on_time", "total_sessions"}``. Raises ``ProviderError`` when an active session has
    planned charging but no forecast is cached for its zone.
    """
    now = _utc(now, "now")
    latest_schedules = latest_schedules or {}
    last_unmet_kwh = last_unmet_kwh or {}
    remaining_from = floor_to_slot(now)
    slot = timedelta(minutes=settings.slot_minutes)
    slot_hours = slot / _HOUR

    rows = db.execute(
        select(Session, Site.grid_zone)
        .join(Session.charger)
        .join(Charger.site)
        .where(Session.status.in_(COUNTED_STATUSES))
        .order_by(Session.id)
    ).all()

    co2_saved_g = 0.0
    cost_saved_inr = 0.0
    sessions_on_time = 0
    # Active sessions: the charging still planned, as (zone, slot_start, kW), priced below.
    remaining: list[tuple[str, datetime, float]] = []
    slots_by_zone: dict[str, set[datetime]] = defaultdict(set)

    for session, zone in rows:
        co2_saved_g += (session.co2_baseline_g or 0.0) - (session.co2_actual_g or 0.0)
        cost_saved_inr += (session.cost_baseline_inr or 0.0) - (session.cost_actual_inr or 0.0)
        if session.status == SESSION_COMPLETED:
            if session.soc_current >= session.soc_target - settings.soc_tolerance:
                sessions_on_time += 1
            continue
        # Active.
        if last_unmet_kwh.get(session.id, 0.0) == 0.0:
            sessions_on_time += 1
        plan = latest_schedules.get(session.id)
        if not plan:
            continue
        for slot_start, kw in plan["slots"]:
            slot_start = _utc(slot_start, "slot_start")
            kw = float(kw)
            if slot_start >= remaining_from and kw > 0:
                remaining.append((zone, slot_start, kw))
                slots_by_zone[zone].add(slot_start)

    ci_by_zone = {zone: _forecast_ci(db, zone, starts) for zone, starts in slots_by_zone.items()}
    for zone, slot_start, kw in remaining:
        energy_kwh = kw * slot_hours
        co2_saved_g -= energy_kwh * ci_by_zone[zone][slot_start]
        cost_saved_inr -= energy_kwh * price_at(slot_start)

    return {
        "co2_saved_kg": co2_saved_g / G_PER_KG,
        "cost_saved_inr": cost_saved_inr,
        "sessions_on_time": sessions_on_time,
        "total_sessions": len(rows),
    }
