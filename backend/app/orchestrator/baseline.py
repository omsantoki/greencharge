"""The baseline shadow simulation (BUILD_SPEC Phase 4) and the site load curve.

The baseline answers, for every real session: *what if this car had charged at full acceptance
rate from plug-in?* Naive chargers do not coordinate, so no site limit is applied. Every savings
number in the product is ``baseline - actual``. This module is pure computation plus reads through
the database session it is handed: it never sends an OCPP command and never talks to a charge
point.

Physics: ``acceptance_kw`` and ``step`` are the spec's "Battery physics" block, EXACT copies of
``simulator/battery.py``. The backend cannot import the simulator (a separate program with its own
requirements file), so the two functions are repeated here verbatim; keep them identical.

Naive charge (``baseline_power_profile``): from ``start`` the car draws
``acceptance_kw(soc, max_kw)``, re-evaluated at the start of every sub-step of at most 1 simulated
minute, and its SoC advances with ``step()``. A sub-step also ends at every slot boundary, so each
one lies inside a single 15-minute bucket, and at the deadline. Charging stops when ``soc_target``
is reached or at the deadline, whichever is first. The sub-step in which the target is reached is
cut at that moment with the same bisection on ``step()`` the simulator uses
(``simulator/charge_point.py``, ``_fraction_to_reach``), so the baseline does not overshoot the
target. Power is what the charger draws from the grid; the battery gains that energy times the
efficiency (``step()``).

Buckets: 15-minute slots aligned with ``app.providers.base.floor_to_slot``. A bucket's kW is its
grid-side energy divided by the full slot length, so ``sum(kW) * 0.25 h`` is the grid energy even
for the partly used first and last slots.

All datetimes are timezone-aware; results are in UTC. A naive datetime raises ``ValueError``.
"""
import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.models import Charger, MeterValue, Session
from app.providers.base import floor_to_slot

# Session.status values (models.Session: active|completed|aborted).
SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"
SESSION_ABORTED = "aborted"
# "Non-aborted" sessions: the ones the load curve and the impact summary count.
COUNTED_STATUSES = (SESSION_ACTIVE, SESSION_COMPLETED)

# Phase 4 design: the naive charge is simulated in 1-minute sub-steps. That is the simulated
# length of the simulator's physics step (1 real second) at the default TIME_SCALE of 60.
SUBSTEP = timedelta(minutes=1)

# Halvings used to find the moment inside the final sub-step at which soc_target is reached: the
# same bisection as simulator/charge_point.py (_fraction_to_reach, BISECTION_ITERATIONS = 40);
# 2**-40 of a sub-step is far below any meaningful time or energy.
BISECTION_ITERATIONS = 40

_HOUR = timedelta(hours=1)


# --------------------------------------------------------------------------------------------
# Battery physics: EXACT copies of simulator/battery.py (the build spec's "Battery physics")
# --------------------------------------------------------------------------------------------


def acceptance_kw(soc: float, max_kw: float) -> float:
    """Charge rate the vehicle will accept at a given SOC.
    Constant-current below 80%, then linear taper to 20% of max at 100%."""
    if soc < 0.80:
        return max_kw
    return max_kw * (1.0 - 0.8 * (soc - 0.80) / 0.20)

def step(soc, battery_kwh, power_kw, dt_hours, efficiency=0.92):
    """Advance SOC by one timestep."""
    delivered_kwh = power_kw * dt_hours * efficiency
    return min(1.0, soc + delivered_kwh / battery_kwh)


# --------------------------------------------------------------------------------------------
# The naive charge
# --------------------------------------------------------------------------------------------


def _require_aware(dt: datetime, name: str) -> None:
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")


def _utc(dt: datetime, name: str = "datetime") -> datetime:
    """``dt`` as aware UTC (raises for a naive value)."""
    _require_aware(dt, name)
    return dt.astimezone(timezone.utc)


def _slot() -> timedelta:
    return timedelta(minutes=settings.slot_minutes)


def _fraction_to_reach(
    soc: float,
    soc_target: float,
    battery_kwh: float,
    power_kw: float,
    dt_hours: float,
    efficiency: float,
) -> float:
    """Smallest fraction f of ``dt_hours`` with step(soc, ..., f * dt_hours) >= soc_target.

    Bisection on ``step()`` itself (monotonic in time), as in the simulator, so the result always
    satisfies the target and no physics formula is duplicated. Only called when the whole
    sub-step reaches the target.
    """
    lo, hi = 0.0, 1.0
    for _ in range(BISECTION_ITERATIONS):
        mid = (lo + hi) / 2.0
        if step(soc, battery_kwh, power_kw, mid * dt_hours, efficiency) >= soc_target:
            hi = mid
        else:
            lo = mid
    return hi


def baseline_power_profile(
    soc_start: float,
    soc_target: float,
    battery_kwh: float,
    max_kw: float,
    start: datetime,
    deadline: datetime,
    efficiency: float = 0.92,
) -> list[tuple[datetime, float]]:
    """Naive charging at ``acceptance_kw()`` from ``start`` until ``soc_target`` or ``deadline``
    (no site limit), simulated in sub-steps of at most 1 minute (see the module docstring).

    Returns contiguous ``(slot_start, average kW)`` buckets, one per 15-minute slot, from
    ``floor_to_slot(start)`` to the last slot with charging; ``slot_start`` is aware UTC and the
    kW is the slot's grid-side energy over the full slot length. Empty when nothing is charged
    (``soc_start >= soc_target``, ``deadline <= start`` or ``max_kw <= 0``).

    Raises ``ValueError`` for a naive datetime, a non-finite number, ``battery_kwh <= 0`` or
    ``efficiency <= 0``.
    """
    start = _utc(start, "start")
    deadline = _utc(deadline, "deadline")
    for name, value in (
        ("soc_start", soc_start),
        ("soc_target", soc_target),
        ("battery_kwh", battery_kwh),
        ("max_kw", max_kw),
        ("efficiency", efficiency),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {value!r}")
    if battery_kwh <= 0:
        raise ValueError(f"battery_kwh must be > 0, got {battery_kwh}")
    if efficiency <= 0:
        raise ValueError(f"efficiency must be > 0, got {efficiency}")

    slot = _slot()
    first_slot = floor_to_slot(start)
    energy_kwh: list[float] = []  # grid-side energy per bucket; index 0 = first_slot
    t = start
    soc = float(soc_start)
    while soc < soc_target and t < deadline:
        power_kw = acceptance_kw(soc, max_kw)
        if power_kw <= 0:
            break
        index = (t - first_slot) // slot
        t_end = min(t + SUBSTEP, first_slot + (index + 1) * slot, deadline)
        dt_hours = (t_end - t) / _HOUR
        new_soc = step(soc, battery_kwh, power_kw, dt_hours, efficiency)
        reached = new_soc >= soc_target
        if reached:  # the target is reached inside this sub-step: charging stops right there
            dt_hours *= _fraction_to_reach(
                soc, soc_target, battery_kwh, power_kw, dt_hours, efficiency
            )
            new_soc = step(soc, battery_kwh, power_kw, dt_hours, efficiency)
        while len(energy_kwh) <= index:
            energy_kwh.append(0.0)
        energy_kwh[index] += power_kw * dt_hours
        soc = new_soc
        if reached:
            break
        t = t_end

    slot_hours = slot / _HOUR
    return [(first_slot + i * slot, e / slot_hours) for i, e in enumerate(energy_kwh)]


def _session_profile(session: Any) -> list[tuple[datetime, float]]:
    """``baseline_power_profile`` of a session: from plug-in, at the session's max_charge_kw."""
    return baseline_power_profile(
        soc_start=session.soc_start,
        soc_target=session.soc_target,
        battery_kwh=session.battery_kwh,
        max_kw=session.max_charge_kw,
        start=session.plugged_in_at,
        deadline=session.deadline,
    )


def _curve_value(curve: list[float], index: int, name: str) -> float:
    """``curve[index]``, repeating the last value past the end; must be finite."""
    value = float(curve[min(index, len(curve) - 1)])
    if not math.isfinite(value):
        raise ValueError(f"{name} has a non-finite value {value!r}")
    return value


def simulate_baseline(
    session: Any, carbon_curve: list[float], price_curve: list[float]
) -> tuple[float, float]:
    """Charge at acceptance_kw() from plug-in until soc_target.
    Returns (co2_g, cost_inr). No site limit applied — naive chargers
    do not coordinate, which is exactly the point.

    ``session`` is a ``models.Session`` (or anything with soc_start, soc_target, battery_kwh,
    max_charge_kw, plugged_in_at and deadline). The naive charge is
    ``baseline_power_profile(...)`` from ``plugged_in_at`` at ``max_charge_kw``, stopping at the
    deadline if the target is not reached by then.

    ``carbon_curve`` (gCO2eq/kWh) and ``price_curve`` (INR/kWh) are 15-minute slots starting at
    ``floor_to_slot(session.plugged_in_at)``; when the charge runs past the end of a curve, its
    last value is repeated. Result: the sum over slots of grid-side energy x CI, and x price.
    Pure computation: never sends OCPP. Raises ``ValueError`` when there is energy to price and a
    curve is empty or holds a non-finite value.
    """
    profile = _session_profile(session)
    if not profile:
        return 0.0, 0.0
    if not carbon_curve or not price_curve:
        raise ValueError("simulate_baseline needs a non-empty carbon_curve and price_curve")
    slot_hours = _slot() / _HOUR
    co2_g = 0.0
    cost_inr = 0.0
    for index, (_, kw) in enumerate(profile):
        energy_kwh = kw * slot_hours
        co2_g += energy_kwh * _curve_value(carbon_curve, index, "carbon_curve")
        cost_inr += energy_kwh * _curve_value(price_curve, index, "price_curve")
    return co2_g, cost_inr


# --------------------------------------------------------------------------------------------
# The site load curve
# --------------------------------------------------------------------------------------------


def _slot_index(ts: datetime, window_start: datetime, slot: timedelta) -> int | None:
    """Index of the slot starting exactly at ``ts``, or None when ``ts`` is not a slot start."""
    index, remainder = divmod(ts - window_start, slot)
    return None if remainder else index


def _plan_slots(plan: Any) -> Iterable[tuple[datetime, float]]:
    """The ``(slot_start, kW)`` pairs of one ``latest_schedules`` entry (UTC slot starts)."""
    if not plan:
        return ()
    return ((_utc(slot_start, "slot_start"), float(kw)) for slot_start, kw in plan["slots"])


def site_load_curve(
    db: DbSession, site: Any, now: datetime, latest_schedules: dict[int, dict]
) -> dict:
    """Optimized vs baseline aggregate load of one site over 96 slots.

    Sessions: the site's non-aborted sessions (status active or completed).
    Window: 96 slots from ``floor_to_slot(earliest plugged_in_at)`` of those sessions, or from
    ``floor_to_slot(now)`` when there are none.

    - ``baseline_kw[t]``: sum of every session's ``baseline_power_profile`` at slot t.
    - ``optimized_kw[t]`` for a slot that ENDS <= now (``is_past``, actual): sum over sessions of
      the AVERAGE power the session drew in that slot, taken from its cumulative energy register
      (Δenergy / slot hours; a session with no reading in the slot adds 0). Averaging the
      instantaneous ``power_kw`` samples instead would let a slot holding one or two samples read
      above a site limit the site never actually exceeded.
    - ``optimized_kw[t]`` for every other slot (plan): sum of the planned power at that
      slot_start in the latest schedule (``latest_schedules[id]["slots"]``, a list of
      ``(slot_start, kW)``) of each session that is still ACTIVE. A completed session charges no
      more, so its last plan is not counted; it contributes through its meter values once a slot
      has ended.

    ``site`` is a ``models.Site`` (``id``, ``max_power_kw``). Reads through ``db`` only.
    Returns ``{"site_id", "max_power_kw", "window_start", "now", "slots": [{"slot_start",
    "optimized_kw", "baseline_kw", "is_past"} x 96], "optimized_peak_kw", "baseline_peak_kw"}``
    with aware UTC datetimes (FastAPI serialises them as ISO-8601 with offset).
    """
    now = _utc(now, "now")
    latest_schedules = latest_schedules or {}
    slot = _slot()
    n_slots = settings.horizon_slots

    sessions = list(
        db.scalars(
            select(Session)
            .join(Session.charger)
            .where(Charger.site_id == site.id, Session.status.in_(COUNTED_STATUSES))
            .order_by(Session.id)
        ).all()
    )
    if sessions:
        earliest = min(_utc(s.plugged_in_at, "plugged_in_at") for s in sessions)
        window_start = floor_to_slot(earliest)
    else:
        window_start = floor_to_slot(now)
    slot_starts = [window_start + i * slot for i in range(n_slots)]
    is_past = [slot_start + slot <= now for slot_start in slot_starts]
    n_past = sum(is_past)  # the past slots are a prefix of the window

    baseline_kw = [0.0] * n_slots
    for session in sessions:
        for slot_start, kw in _session_profile(session):
            index = _slot_index(slot_start, window_start, slot)
            if index is not None and 0 <= index < n_slots:
                baseline_kw[index] += kw

    optimized_kw = [0.0] * n_slots

    # Past slots: actual, from the meter values.
    if sessions and n_past:
        past_end = window_start + n_past * slot
        slot_hours = slot.total_seconds() / 3600.0
        rows = db.execute(
            select(MeterValue.session_id, MeterValue.ts, MeterValue.energy_kwh)
            .where(
                MeterValue.session_id.in_([s.id for s in sessions]),
                MeterValue.ts < past_end,
            )
            .order_by(MeterValue.session_id, MeterValue.ts)
        ).all()
        readings: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
        for row in rows:
            readings[row.session_id].append(
                (_utc(row.ts, "meter value ts"), float(row.energy_kwh))
            )
        for session in sessions:
            # Each reading closes an interval that began at the previous reading (or at plug-in,
            # where the register is 0). A reading period need not line up with the 15-minute slots,
            # so spread the interval's energy across the slots it covers in proportion to the time
            # it spent in each. Crediting it all to the slot holding the later reading would let a
            # slot collect two reading periods and read up to a third above the real power.
            previous_ts = _utc(session.plugged_in_at, "plugged_in_at")
            previous_kwh = 0.0
            for ts, energy_kwh in readings.get(session.id, ()):
                delivered_kwh = energy_kwh - previous_kwh
                span_s = (ts - previous_ts).total_seconds()
                if delivered_kwh > 0.0 and span_s > 0.0:  # a register reset would read negative
                    first = max(0, math.floor((previous_ts - window_start) / slot))
                    last = min(n_past, math.ceil((ts - window_start) / slot))
                    for index in range(first, last):
                        slot_from = window_start + index * slot
                        overlap_s = (
                            min(ts, slot_from + slot) - max(previous_ts, slot_from)
                        ).total_seconds()
                        if overlap_s > 0.0:
                            share = delivered_kwh * (overlap_s / span_s)
                            optimized_kw[index] += share / slot_hours
                previous_ts, previous_kwh = ts, energy_kwh

    # Current and future slots: the latest plan of each active session.
    for session in sessions:
        if session.status != SESSION_ACTIVE:
            continue
        for slot_start, kw in _plan_slots(latest_schedules.get(session.id)):
            index = _slot_index(slot_start, window_start, slot)
            if index is not None and n_past <= index < n_slots:
                optimized_kw[index] += kw

    return {
        "site_id": site.id,
        "max_power_kw": site.max_power_kw,
        "window_start": window_start,
        "now": now,
        "slots": [
            {
                "slot_start": slot_starts[i],
                "optimized_kw": optimized_kw[i],
                "baseline_kw": baseline_kw[i],
                "is_past": is_past[i],
            }
            for i in range(n_slots)
        ],
        "optimized_peak_kw": max(optimized_kw, default=0.0),
        "baseline_peak_kw": max(baseline_kw, default=0.0),
    }
