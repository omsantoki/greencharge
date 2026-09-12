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

Phase 6 adds the same numbers for ONE session, for the driver's screens:

- ``session_impact`` -- that session's savings (the rules above, applied to it alone), its ETA,
  the SoC its plan projects at the deadline, and the green/grey split of the energy it has drawn.
- ``override_preview`` -- what "charge at full power now" would cost the session against its
  current plan, so the driver sees the price of the override BEFORE confirming it.

Both re-use the tick's own helpers (``loop.build_available_mask``, ``loop._max_now_schedule``,
the acceptance ceiling, the optimizer's efficiency), so a preview says what the next tick will
really do, and both price future charging with the same cached FORECAST as ``impact_summary``.

The green/grey split is a PRESENTATION choice, not a measurement: metered energy the session drew
in a slot whose forecast carbon intensity is below the mean over the session's own horizon
(plug-in slot to deadline slot) counts as green, the rest as grey. No grid tells us which
electrons were renewable; this only says "cleaner than this session's average hour, or not".
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.models import Charger, GridData, MeterValue, Session, Site
from app.orchestrator.baseline import (
    COUNTED_STATUSES,
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    step,
)
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


# --------------------------------------------------------------------------------------------
# Phase 6: the same numbers for one session (the driver PWA)
# --------------------------------------------------------------------------------------------


def _slot() -> timedelta:
    return timedelta(minutes=settings.slot_minutes)


def _session_charger(db: DbSession, session: Any) -> tuple[str, str]:
    """(grid zone, charger status) of the session's charger.

    Falls back to the configured zone and an empty status when the charger row cannot be read,
    exactly as the OCPP handlers fall back for their zone lookup.
    """
    row = db.execute(
        select(Site.grid_zone, Charger.status)
        .join(Charger, Charger.site_id == Site.id)
        .where(Charger.id == session.charger_id)
    ).first()
    if row is None:
        return settings.electricity_maps_zone, ""
    return str(row[0]), str(row[1])


def _plan_remaining(
    latest_schedule: dict | None, remaining_from: datetime
) -> list[tuple[datetime, float]]:
    """The charging a plan still has ahead of it: ``(slot_start, kW)`` with
    ``slot_start >= remaining_from`` and kW > 0, earliest first.

    These are exactly the slots ``impact_summary`` prices for an active session; slots of 0 kW
    are dropped because they add nothing to any of the sums.
    """
    if not latest_schedule:
        return []
    slots = [
        (_utc(slot_start, "slot_start"), float(kw))
        for slot_start, kw in latest_schedule["slots"]
    ]
    return sorted((s, kw) for s, kw in slots if s >= remaining_from and kw > 0)


def _price_slots(
    slots: list[tuple[datetime, float]], ci_by_slot: dict[datetime, float], slot_hours: float
) -> tuple[float, float]:
    """(gCO2, INR) of charging ``slots``, with the FORECAST carbon intensity in ``ci_by_slot``
    and the tariff -- the way ``impact_summary`` prices an active session's remaining plan."""
    co2_g = 0.0
    cost_inr = 0.0
    for slot_start, kw in slots:
        energy_kwh = kw * slot_hours
        co2_g += energy_kwh * ci_by_slot[slot_start]
        cost_inr += energy_kwh * price_at(slot_start)
    return co2_g, cost_inr


def _plan_eta(
    slots: list[tuple[datetime, float]],
    energy_needed_kwh: float,
    efficiency: float,
    tolerance: float,
) -> datetime | None:
    """End of the slot at which the cumulative planned energy of ``slots`` first covers
    ``energy_needed_kwh``, or None when the plan never covers it.

    Energy is counted battery-side (kW x slot hours x efficiency), as the optimizer's energy
    constraint counts it, so this is the moment the car reaches its target SoC. None as well
    when no energy is needed any more: there is no arrival left to wait for.
    """
    if energy_needed_kwh <= tolerance:
        return None
    slot = _slot()
    slot_hours = slot / _HOUR
    cumulative = 0.0
    for slot_start, kw in slots:
        cumulative += kw * slot_hours * efficiency
        if cumulative >= energy_needed_kwh - tolerance:
            return slot_start + slot
    return None


def _projected_soc(
    session: Any, slots: list[tuple[datetime, float]], efficiency: float
) -> float:
    """The SoC the plan leaves the car at, from ``soc_current`` through ``slots`` with the
    spec's battery ``step()``. The plan holds no slot past the deadline, so charging every
    remaining slot is the projection at the deadline."""
    slot_hours = _slot() / _HOUR
    soc = float(session.soc_current)
    for _, kw in slots:
        soc = step(soc, session.battery_kwh, kw, slot_hours, efficiency)
    return soc


def _delivered_by_slot(db: DbSession, session: Any) -> dict[datetime, float]:
    """Grid-side kWh the session has drawn in each 15-minute slot, from its meter values.

    Each reading closes an interval that began at the previous reading (at plug-in the register
    is 0) and the interval's energy is spread over the slots it covers in proportion to the time
    it spent in each -- the same rule the past part of ``baseline.site_load_curve`` uses, because
    a 10-minute reading period does not line up with the 15-minute slots. Slots with no energy
    are absent; the values sum to the last reading's cumulative register.
    """
    rows = db.execute(
        select(MeterValue.ts, MeterValue.energy_kwh)
        .where(MeterValue.session_id == session.id)
        .order_by(MeterValue.ts, MeterValue.id)
    ).all()
    slot = _slot()
    delivered: dict[datetime, float] = defaultdict(float)
    previous_ts = _utc(session.plugged_in_at, "plugged_in_at")
    previous_kwh = 0.0
    for row in rows:
        ts = _utc(row.ts, "meter value ts")
        energy_kwh = float(row.energy_kwh)
        delta_kwh = energy_kwh - previous_kwh
        span_s = (ts - previous_ts).total_seconds()
        if delta_kwh > 0.0 and span_s > 0.0:  # a register reset would read negative
            slot_start = floor_to_slot(previous_ts)
            while slot_start < ts:
                overlap_s = (
                    min(ts, slot_start + slot) - max(previous_ts, slot_start)
                ).total_seconds()
                if overlap_s > 0.0:
                    delivered[slot_start] += delta_kwh * (overlap_s / span_s)
                slot_start += slot
        previous_ts, previous_kwh = ts, energy_kwh
    return dict(delivered)


def _session_horizon(session: Any) -> list[datetime]:
    """The session's own horizon: the slots from the one holding its plug-in to the one holding
    its deadline, at most ``settings.horizon_slots`` of them (a deadline beyond the 24-hour
    horizon does not stretch the reference window). Always at least one slot."""
    slot = _slot()
    start = floor_to_slot(_utc(session.plugged_in_at, "plugged_in_at"))
    end = floor_to_slot(_utc(session.deadline, "deadline"))
    n_slots = int((end - start) // slot) + 1
    n_slots = max(1, min(n_slots, settings.horizon_slots))
    return [start + i * slot for i in range(n_slots)]


def session_impact(
    db: DbSession, session: Any, now: datetime, latest_schedule: dict | None
) -> dict:
    """One session's savings, ETA, projected SoC and green/grey split (the driver's screens).

    ``session`` is a ``models.Session`` row; ``latest_schedule`` its newest plan
    (``{"computed_at", "slots": [(slot_start, kW)]}`` from ``loop.get_latest_schedules()``, or
    None when it has none yet). Reads through ``db`` only; sends nothing.

    Savings follow ``impact_summary`` exactly: baseline - actual for a session that is no longer
    active, and baseline - (actual + the remaining plan priced with the cached forecast) while it
    is active. ``eta`` is the end of the planned slot that first covers the energy still needed
    (null when the plan never covers it, and when nothing is needed any more).
    ``projected_soc_at_deadline`` runs the remaining plan through the battery model from
    ``soc_current``. ``on_time`` is that projection reaching ``soc_target`` (within
    ``settings.soc_tolerance``) -- for a completed session that is the summary's completed rule,
    and for an active one it is the summary's "the last tick left no unmet energy"; an active
    session with no plan at all is on time, exactly as ``GET /api/sessions/active`` treats a
    session the last tick did not plan. ``green_kwh``/``grey_kwh`` split the METERED energy (see
    the module docstring); they sum to what the meter has reported, which is
    ``energy_delivered_kwh`` except for the final StopTransaction reading.

    Returns EXACTLY ``{"session_id", "co2_saved_g", "cost_saved_inr", "co2_actual_g",
    "co2_baseline_g", "green_kwh", "grey_kwh", "eta", "projected_soc_at_deadline", "on_time",
    "energy_needed_kwh", "energy_delivered_kwh"}``, with ``eta`` an ISO-8601 string or None.
    Raises ``ProviderError`` when there is charging to price or split but no forecast is cached
    for the session's zone.
    """
    # Lazy import: app/orchestrator/__init__.py explains the import cycle it avoids. The tick's
    # own helpers are used so these numbers are the ones the orchestrator acts on.
    from app.orchestrator import loop as orchestrator_loop

    now = _utc(now, "now")
    slot_hours = _slot() / _HOUR
    zone, _charger_status = _session_charger(db, session)
    efficiency = orchestrator_loop.EFFICIENCY

    energy_needed_kwh = orchestrator_loop._energy_needed_kwh(
        session.soc_target, session.soc_current, session.battery_kwh
    )
    is_active = session.status == SESSION_ACTIVE
    # A session that is no longer active charges no more, so it has no remaining plan to price.
    remaining = _plan_remaining(latest_schedule, floor_to_slot(now)) if is_active else []
    delivered_by_slot = _delivered_by_slot(db, session)
    horizon = _session_horizon(session) if delivered_by_slot else []

    wanted = {slot_start for slot_start, _ in remaining} | set(horizon) | set(delivered_by_slot)
    # _forecast_ci speaks for impact_summary, whose only use of the forecast is the remaining plan
    # of ACTIVE sessions. Here it also splits the delivered energy of a session that has finished,
    # so say what is really missing rather than repeat a reason that would not apply.
    try:
        ci_by_slot = _forecast_ci(db, zone, wanted) if wanted else {}
    except ProviderError as exc:
        raise ProviderError(
            f"No carbon-intensity forecast is cached for zone {zone}, so session {session.id} "
            f"cannot be accounted for."
        ) from exc

    planned_co2_g, planned_cost_inr = _price_slots(remaining, ci_by_slot, slot_hours)
    co2_saved_g = (session.co2_baseline_g or 0.0) - (session.co2_actual_g or 0.0) - planned_co2_g
    cost_saved_inr = (
        (session.cost_baseline_inr or 0.0) - (session.cost_actual_inr or 0.0) - planned_cost_inr
    )

    green_kwh = 0.0
    grey_kwh = 0.0
    if delivered_by_slot:
        mean_ci = sum(ci_by_slot[slot_start] for slot_start in horizon) / len(horizon)
        for slot_start, energy_kwh in delivered_by_slot.items():
            if ci_by_slot[slot_start] < mean_ci:
                green_kwh += energy_kwh
            else:
                grey_kwh += energy_kwh

    eta = _plan_eta(remaining, energy_needed_kwh, efficiency, orchestrator_loop.ZERO_TOLERANCE)
    projected_soc = _projected_soc(session, remaining, efficiency)
    on_time = (
        True
        if is_active and not latest_schedule
        else projected_soc >= session.soc_target - settings.soc_tolerance
    )

    return {
        "session_id": session.id,
        "co2_saved_g": co2_saved_g,
        "cost_saved_inr": cost_saved_inr,
        "co2_actual_g": session.co2_actual_g or 0.0,
        "co2_baseline_g": session.co2_baseline_g or 0.0,
        "green_kwh": green_kwh,
        "grey_kwh": grey_kwh,
        "eta": None if eta is None else eta.isoformat(),
        "projected_soc_at_deadline": projected_soc,
        "on_time": on_time,
        "energy_needed_kwh": energy_needed_kwh,
        "energy_delivered_kwh": session.energy_delivered_kwh or 0.0,
    }


def override_preview(
    db: DbSession, session: Any, now: datetime, latest_schedule: dict | None
) -> dict:
    """What "I'm leaving now" would cost this session, against the plan it is following.

    The override sets the session's manual limit to ``max_charge_kw`` x 1000 W, so the next tick
    plans it as "that draw in every available slot until the energy it needs is covered". This
    builds exactly that plan -- ``loop._max_now_schedule`` with the tick's own acceptance ceiling
    and availability mask -- and prices it against the remaining part of the current plan, both
    with the cached FORECAST carbon intensity and the tariff, never with a guess.

    ``extra_co2_g`` / ``extra_cost_inr`` are max-now minus planned, so they are what the choice
    ADDS (negative if charging now happened to be the cleaner moment). ``eta_now`` and
    ``eta_planned`` are the two arrival times (``_plan_eta``), ISO-8601 strings or None.
    ``limit_w`` is the limit the override would set, the same one POST /api/sessions/{id}/override
    sends. A session that is not active (or whose charger is faulted) can charge in no slot, so
    everything is zero and both ETAs are None.

    Returns EXACTLY ``{"session_id", "extra_co2_g", "extra_cost_inr", "eta_now", "eta_planned",
    "limit_w"}``. Reads through ``db`` only; sends nothing and changes nothing. Raises
    ``ProviderError`` when there is charging to price but no forecast is cached for the zone.
    """
    # Lazy import: see session_impact.
    from app.orchestrator import loop as orchestrator_loop

    now = _utc(now, "now")
    slot = _slot()
    slot_hours = slot / _HOUR
    horizon_start = floor_to_slot(now)
    zone, charger_status = _session_charger(db, session)
    efficiency = orchestrator_loop.EFFICIENCY
    is_active = session.status == SESSION_ACTIVE

    limit_w = session.max_charge_kw * orchestrator_loop.W_PER_KW
    energy_needed_kwh = orchestrator_loop._energy_needed_kwh(
        session.soc_target, session.soc_current, session.battery_kwh
    )
    # The tick's own rule: the charge point draws min(limit, what the vehicle accepts now).
    max_kw = orchestrator_loop._power_ceiling_kw(session.soc_current, session.max_charge_kw)
    limit_kw = limit_w / orchestrator_loop.W_PER_KW
    kw = min(limit_kw, max_kw) if math.isfinite(limit_kw) and limit_kw > 0 else 0.0
    if not is_active or charger_status == orchestrator_loop.FAULTED:
        available = [False] * settings.horizon_slots
    else:
        available = orchestrator_loop.build_available_mask(
            now, _utc(session.deadline, "deadline"), horizon_start
        )
    max_now_kw, _unmet_kwh = orchestrator_loop._max_now_schedule(
        energy_needed_kwh, kw, available
    )
    max_now = [
        (horizon_start + i * slot, power_kw)
        for i, power_kw in enumerate(max_now_kw)
        if power_kw > 0
    ]
    planned = _plan_remaining(latest_schedule, horizon_start) if is_active else []

    wanted = {slot_start for slot_start, _ in max_now} | {slot_start for slot_start, _ in planned}
    ci_by_slot = _forecast_ci(db, zone, wanted) if wanted else {}
    now_co2_g, now_cost_inr = _price_slots(max_now, ci_by_slot, slot_hours)
    planned_co2_g, planned_cost_inr = _price_slots(planned, ci_by_slot, slot_hours)

    tolerance = orchestrator_loop.ZERO_TOLERANCE
    eta_now = _plan_eta(max_now, energy_needed_kwh, efficiency, tolerance)
    eta_planned = _plan_eta(planned, energy_needed_kwh, efficiency, tolerance)
    return {
        "session_id": session.id,
        "extra_co2_g": now_co2_g - planned_co2_g,
        "extra_cost_inr": now_cost_inr - planned_cost_inr,
        "eta_now": None if eta_now is None else eta_now.isoformat(),
        "eta_planned": None if eta_planned is None else eta_planned.isoformat(),
        "limit_w": limit_w,
    }
