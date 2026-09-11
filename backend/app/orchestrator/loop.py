"""The orchestration loop (Phase 4): the model-predictive control tick that wires the grid data
(Phase 1), the OCPP layer (Phase 2) and the optimizer (Phase 3) into one live control loop.

The tick (spec, "The tick"), every ``settings.tick_minutes`` SIMULATED minutes and on demand:

    1. Load every session whose status is "active" (with its charger and site).
    2. energy_needed = max(0, (soc_target - soc_current) * battery_kwh). The plan's power
       ceiling is acceptance_kw(soc_current, max_charge_kw): what the vehicle accepts now.
    3. The ``available`` mask from now to the deadline on the 96-slot horizon that starts at
       floor_to_slot(now) (``build_available_mask``). A charger whose status is "Faulted" is
       unavailable in every slot.
    4. The carbon forecast (``provider.get_forecast`` resampled onto the horizon with
       ``resample_to_slots``) and the tariff (``price_curve``), 96 slots each.
    5. ``optimize()`` in a worker thread, once per site, for the sessions that need a plan.
    6. Persist every active session's 96-slot plan to ``schedules`` (a new version every tick;
       history is kept).
    7. Send each session its slot-0 power as SetChargingProfile, an integer number of watts.
       ONLY slot 0 is executed; the rest of the plan is revised at the next tick (MPC).
    8. Log one INFO line: reason, sessions, solve status, solve time.

Which sessions are optimised: a session needs a plan when it has no manual limit, still needs
energy, can draw power and has at least one available slot. The others get an all-zero plan
(slot 0 = 0 W is still sent) and, when they still need energy, that energy is reported unmet.
When no session of any site needs a plan, ``optimize()`` is not called and the tick status is
"skipped". Safety energy is always 0.0 (implementation contract 6c: the spec gives no rule).

Manual limits (``registry.manual_limits_w``: operator override and debug set-limit) take the
session out of the LP. Its manual draw -- min(limit, the vehicle's acceptance now), or 0 when it
cannot draw at all (target reached, faulted, past its deadline) -- is subtracted from the site
limit the LP gets, the manual limit itself is re-sent every tick, and a "max now" plan (that draw
in every available slot until energy_needed is covered, the last slot partial) is persisted so
the Gantt shows it.

Tick status: the optimizer's "optimal" | "relaxed" | "infeasible" (the worst over the sites),
"skipped" (nothing to optimise), "error" (the tick failed; charge points keep their last limits
and ``state.last_tick`` keeps the previous tick) or "abandoned" (``reset_state()`` ran while the
tick was in flight, so its results were discarded).

Concurrency:
- Ticks never overlap: one ``asyncio.Lock``. ``tick()`` awaits it; ``request_tick()`` starts a
  tick in the background and coalesces requests into the one that is queued (not yet started).
- DB work runs in worker threads (``asyncio.to_thread``), one short transaction per helper.
- ``state`` is changed on the event-loop thread (``set_weights``/``reset_state`` may run
  anywhere) by replacing whole attributes, never by mutating a published dict, so readers in
  other threads (API endpoints in the threadpool) always see a consistent value. Treat
  everything read from ``state`` as read-only.
- DEADLOCK RULE (implementation contract 5a): an OCPP ``@on`` handler must never await
  ``tick()`` (the tick awaits outbound calls whose replies that handler's receive loop would have
  to deliver). Handlers call ``request_tick()`` or create a task for ``on_session_started()``.
"""
import asyncio
import logging
import math
import time
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from ocpp.v16.enums import ChargePointStatus, ChargingProfileStatus
from sqlalchemy import func, insert, select, update

from app.clock import clock
from app.config import settings
from app.db import SessionLocal
from app.models import Charger, Schedule, Session, Site
from app.ocpp import registry
from app.optimizer.engine import (
    STATUS_INFEASIBLE,
    STATUS_OPTIMAL,
    STATUS_RELAXED,
    ZERO_TOLERANCE,
    optimize,
)
from app.optimizer.types import OptimizerInput, OptimizerResult, SessionInput
from app.orchestrator.baseline import acceptance_kw, simulate_baseline
from app.providers import get_provider
from app.providers.base import floor_to_slot, resample_to_slots
from app.providers.tariff import price_curve

logger = logging.getLogger("greencharge.orchestrator.loop")

# Session.status of a session in progress (models.Session: active|completed|aborted).
SESSION_ACTIVE = "active"
# Charger status (OCPP ChargePointStatus) that makes a session unavailable in every slot.
FAULTED = ChargePointStatus.faulted.value

W_PER_KW = 1000.0
MINUTES_PER_HOUR = 60
SECONDS_PER_MINUTE = 60
MS_PER_S = 1000.0

# The horizon: settings.horizon_slots (96) slots of settings.slot_minutes (15) minutes.
N_SLOTS = settings.horizon_slots
SLOT = timedelta(minutes=settings.slot_minutes)
SLOT_HOURS = settings.slot_minutes / MINUTES_PER_HOUR

# The optimizer's own defaults (spec, OptimizerInput): alpha 1.0, beta 0.001, efficiency 0.92.
_OPTIMIZER_DEFAULTS = {f.name: f.default for f in fields(OptimizerInput) if f.default is not MISSING}
DEFAULT_ALPHA: float = _OPTIMIZER_DEFAULTS["alpha"]
DEFAULT_BETA: float = _OPTIMIZER_DEFAULTS["beta"]
EFFICIENCY: float = _OPTIMIZER_DEFAULTS["efficiency"]

# Implementation contract 6c: the spec gives no rule for (C4), so no safety energy is required.
SAFETY_ENERGY_KWH = 0.0

# Tick statuses besides the optimizer's own "optimal" | "relaxed" | "infeasible".
STATUS_SKIPPED = "skipped"  # no session needed optimising, optimize() was not called
STATUS_ERROR = "error"  # the tick failed; nothing was sent
STATUS_ABANDONED = "abandoned"  # reset_state() ran during the tick; its results were discarded
# Worst status wins when several sites are optimised in one tick.
_STATUS_SEVERITY = {STATUS_OPTIMAL: 0, STATUS_RELAXED: 1, STATUS_INFEASIBLE: 2}

REASON_SCHEDULED = "scheduled"
REASON_SESSION_START = "session_start"
TICK_JOB_ID = "greencharge-orchestrator-tick"


# --------------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------------


@dataclass
class OrchestratorState:
    """What the orchestrator knows between ticks (in memory; a restart forgets it).

    - ``alpha`` / ``beta``: the optimizer weights (defaults = OptimizerInput's 1.0 / 0.001).
    - ``last_tick``: the last completed tick, ``{"computed_at", "reason", "n_sessions",
      "status", "solve_ms", "unmet_kwh": {session_id: kWh}}`` (None before the first one).
    - ``latest_schedules``: session_id -> ``{"computed_at": datetime, "slots":
      [(slot_start, kW)] x 96}``, the newest plan of every session planned since the last reset.
    """

    alpha: float = DEFAULT_ALPHA
    beta: float = DEFAULT_BETA
    last_tick: dict | None = None
    latest_schedules: dict[int, dict] = field(default_factory=dict)


# The process-wide orchestrator state. Import it and read its attributes; never rebind it.
state = OrchestratorState()

_tick_lock = asyncio.Lock()
# Bumped by reset_state(). A tick (or baseline) that sees it change drops its results, so a
# tick in flight during a demo reset cannot write plans for sessions that no longer exist.
_generation = 0
# The event loop the orchestrator runs on (set by start_scheduler and by every tick), so that
# request_tick() also works when called from a worker thread.
_loop: asyncio.AbstractEventLoop | None = None
# Strong references to fire-and-forget tasks (asyncio keeps only weak ones).
_background: set[asyncio.Task] = set()


class _TickRequest:
    """A requested tick that has not taken the lock yet; later requests add their reasons."""

    __slots__ = ("reasons",)

    def __init__(self, reason: str) -> None:
        self.reasons = [reason]


_queued: _TickRequest | None = None


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")


def build_available_mask(
    now: datetime,
    deadline: datetime,
    horizon_start: datetime,
    n_slots: int = N_SLOTS,
    slot_minutes: int = settings.slot_minutes,
) -> list[bool]:
    """Which horizon slots a session can charge in, from now until its deadline.

    Slot t covers ``horizon_start + t*slot_minutes`` .. ``+ (t+1)*slot_minutes``.
    Slot 0 (the current slot) is available while ``now < deadline``; slot t > 0 only when the
    whole slot ends by the deadline (``slot_start + slot_minutes <= deadline``), so no energy is
    planned in a slot the car leaves during. A deadline at or before ``now`` gives all False.

    A charger whose status is "Faulted" is unavailable in every slot; that rule is applied by
    the tick, which knows the charger's status (this function only sees times).
    """
    _require_aware(now, "now")
    _require_aware(deadline, "deadline")
    _require_aware(horizon_start, "horizon_start")
    if n_slots < 0:
        raise ValueError(f"n_slots must be >= 0, got {n_slots}")
    if slot_minutes <= 0:
        raise ValueError(f"slot_minutes must be positive, got {slot_minutes}")
    step = timedelta(minutes=slot_minutes)
    return [
        now < deadline if t == 0 else horizon_start + (t + 1) * step <= deadline
        for t in range(n_slots)
    ]


def _energy_needed_kwh(soc_target: float, soc_current: float, battery_kwh: float) -> float:
    """Battery-side energy still needed: max(0, (soc_target - soc_current) * battery_kwh)."""
    value = (soc_target - soc_current) * battery_kwh
    return value if math.isfinite(value) and value > 0 else 0.0


def _power_ceiling_kw(soc_current: float, max_charge_kw: float) -> float:
    """acceptance_kw(soc_current, max_charge_kw), never negative (the optimizer requires >= 0)."""
    value = acceptance_kw(soc_current, max_charge_kw)
    return value if math.isfinite(value) and value > 0 else 0.0


def _max_now_schedule(
    energy_needed_kwh: float, kw: float, available: list[bool]
) -> tuple[list[float], float]:
    """The "max now" plan of a manually limited session and the energy it leaves unmet.

    ``kw`` in every available slot, in order, until ``energy_needed_kwh`` (battery side, i.e.
    grid kWh x efficiency, as in the optimizer's (C1)) is covered; the last slot is partial.
    """
    per_slot_kwh = kw * SLOT_HOURS * EFFICIENCY
    remaining = energy_needed_kwh
    plan: list[float] = []
    for ok in available:
        if not ok or remaining <= ZERO_TOLERANCE or per_slot_kwh <= 0:
            plan.append(0.0)
        elif per_slot_kwh >= remaining:
            plan.append(remaining / (SLOT_HOURS * EFFICIENCY))
            remaining = 0.0
        else:
            plan.append(kw)
            remaining -= per_slot_kwh
    unmet = remaining if remaining > ZERO_TOLERANCE else 0.0
    return plan, unmet


def _forecast_hours(n_slots: int) -> int:
    """Whole hours of forecast that cover ``n_slots`` slots (24 for the 96-slot horizon)."""
    return max(1, math.ceil(n_slots * settings.slot_minutes / MINUTES_PER_HOUR))


def _to_watts(kw: float) -> int:
    """kW -> an integer number of watts (SetChargingProfile limits are sent as integers)."""
    return int(round(kw * W_PER_KW))


# --------------------------------------------------------------------------------------------
# Database work (sync; run in worker threads, one short transaction each)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ActiveSession:
    """The columns one tick needs from an active session, its charger and its site."""

    id: int
    ocpp_transaction_id: int | None
    ocpp_id: str
    charger_status: str
    site_id: int
    site_max_power_kw: float
    grid_zone: str
    battery_kwh: float
    max_charge_kw: float
    soc_current: float
    soc_target: float
    deadline: datetime


def _db_load_active_sessions() -> list[_ActiveSession]:
    stmt = (
        select(
            Session.id,
            Session.ocpp_transaction_id,
            Charger.ocpp_id,
            Charger.status,
            Site.id,
            Site.max_power_kw,
            Site.grid_zone,
            Session.battery_kwh,
            Session.max_charge_kw,
            Session.soc_current,
            Session.soc_target,
            Session.deadline,
        )
        .join(Charger, Session.charger_id == Charger.id)
        .join(Site, Charger.site_id == Site.id)
        .where(Session.status == SESSION_ACTIVE)
        .order_by(Session.id)
    )
    with SessionLocal() as db:
        rows = db.execute(stmt).all()
    return [_ActiveSession(*row) for row in rows]


def _db_insert_schedules(rows: list[dict[str, Any]]) -> None:
    """Bulk-insert one tick's plan rows (a new version; older versions are kept)."""
    if not rows:
        return
    with SessionLocal() as db:
        db.execute(insert(Schedule), rows)
        db.commit()


def _db_latest_schedules(exclude: set[int]) -> dict[int, dict]:
    """The newest persisted plan (latest computed_at) of every session not in ``exclude``."""
    latest = select(
        Schedule.session_id, func.max(Schedule.computed_at).label("computed_at")
    ).group_by(Schedule.session_id)
    if exclude:
        latest = latest.where(Schedule.session_id.not_in(sorted(exclude)))
    latest = latest.subquery()
    stmt = (
        select(Schedule.session_id, Schedule.computed_at, Schedule.slot_start, Schedule.power_kw)
        .join(
            latest,
            (Schedule.session_id == latest.c.session_id)
            & (Schedule.computed_at == latest.c.computed_at),
        )
        .order_by(Schedule.session_id, Schedule.slot_start)
    )
    with SessionLocal() as db:
        rows = db.execute(stmt).all()
    out: dict[int, dict] = {}
    for row in rows:
        entry = out.setdefault(
            row.session_id,
            {"computed_at": row.computed_at.astimezone(timezone.utc), "slots": []},
        )
        entry["slots"].append((row.slot_start.astimezone(timezone.utc), row.power_kw))
    return out


def _db_load_session_for_baseline(session_id: int) -> tuple[Session, str] | None:
    """The Session row (detached, columns loaded) and its site's grid zone, or None."""
    stmt = (
        select(Session, Site.grid_zone)
        .join(Charger, Session.charger_id == Charger.id)
        .join(Site, Charger.site_id == Site.id)
        .where(Session.id == session_id)
    )
    with SessionLocal() as db:
        row = db.execute(stmt).first()
    return None if row is None else (row[0], row[1])


def _db_store_baseline(
    session_id: int, plugged_in_at: datetime, co2_g: float, cost_inr: float
) -> bool:
    """Store the baseline on the session; False when the row is gone or is another session
    (ids restart after a demo reset, so ``plugged_in_at`` must match too)."""
    with SessionLocal() as db:
        result = db.execute(
            update(Session)
            .where(Session.id == session_id, Session.plugged_in_at == plugged_in_at)
            .values(co2_baseline_g=co2_g, cost_baseline_inr=cost_inr)
        )
        found = result.rowcount > 0
        db.commit()
    return found


# --------------------------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------------------------


@dataclass
class _Plan:
    """One active session's inputs and its plan for this tick."""

    session: _ActiveSession
    energy_needed_kwh: float
    max_kw: float
    available: list[bool]
    manual_limit_w: float | None  # the manual limit when the session is manually limited
    manual_kw: float  # what a manually limited session draws under its limit (0 otherwise)
    optimise: bool  # True when the LP plans this session
    schedule_kw: list[float]
    unmet_kwh: float


def _plan_session(
    s: _ActiveSession, now: datetime, horizon_start: datetime, manual_limit_w: float | None
) -> _Plan:
    energy = _energy_needed_kwh(s.soc_target, s.soc_current, s.battery_kwh)
    max_kw = _power_ceiling_kw(s.soc_current, s.max_charge_kw)
    if s.charger_status == FAULTED:
        available = [False] * N_SLOTS
    else:
        available = build_available_mask(now, s.deadline, horizon_start)

    if manual_limit_w is not None:
        limit_kw = float(manual_limit_w) / W_PER_KW
        # The charge point draws min(limit, acceptance); acceptance only falls as SoC rises.
        kw = min(limit_kw, max_kw) if math.isfinite(limit_kw) and limit_kw > 0 else 0.0
        schedule, unmet = _max_now_schedule(energy, kw, available)
        return _Plan(
            session=s,
            energy_needed_kwh=energy,
            max_kw=max_kw,
            available=available,
            manual_limit_w=float(manual_limit_w),
            # Occupies the site connection only while it can actually draw.
            manual_kw=kw if any(p > 0 for p in schedule) else 0.0,
            optimise=False,
            schedule_kw=schedule,
            unmet_kwh=unmet,
        )

    optimise = energy > 0 and max_kw > 0 and any(available)
    return _Plan(
        session=s,
        energy_needed_kwh=energy,
        max_kw=max_kw,
        available=available,
        manual_limit_w=None,
        manual_kw=0.0,
        optimise=optimise,
        schedule_kw=[0.0] * N_SLOTS,
        # Not optimisable but still short of its target: that energy cannot be delivered.
        # (Cleaned like the optimizer's output: impact_summary tests unmet == 0.0 exactly.)
        unmet_kwh=0.0 if optimise or energy <= ZERO_TOLERANCE else energy,
    )


async def _carbon_curve(zone: str, start: datetime, n_slots: int) -> list[float]:
    """Forecast carbon intensity (gCO2/kWh) for ``n_slots`` slots from ``start``."""
    points = await get_provider().get_forecast(zone, _forecast_hours(n_slots))
    curve = [
        float(p.carbon_intensity)
        for p in resample_to_slots(points, start, n_slots, settings.slot_minutes)
    ]
    if not all(math.isfinite(v) for v in curve):
        raise ValueError(f"the carbon forecast for {zone} contains a NaN or infinite value")
    return curve


def _timed_optimize(inp: OptimizerInput) -> tuple[OptimizerResult, float]:
    """optimize() and its solve time in ms (runs in a worker thread)."""
    started = time.perf_counter()
    result = optimize(inp)
    return result, (time.perf_counter() - started) * MS_PER_S


async def _optimise_sites(
    plans: list[_Plan], horizon_start: datetime, alpha: float, beta: float
) -> tuple[str, float]:
    """Run the LP once per site for its sessions that need a plan; fill in their plans.

    Returns (tick status, total solve ms). The forecast and tariff are fetched only when some
    site has a session to optimise.
    """
    by_site: dict[int, list[_Plan]] = {}
    for plan in plans:
        by_site.setdefault(plan.session.site_id, []).append(plan)

    statuses: list[str] = []
    solve_ms = 0.0
    carbon_by_zone: dict[str, list[float]] = {}
    prices: list[float] | None = None
    for site_plans in by_site.values():
        to_optimise = [p for p in site_plans if p.optimise]
        if not to_optimise:
            continue
        site = site_plans[0].session
        manual_kw = math.fsum(p.manual_kw for p in site_plans)
        if site.grid_zone not in carbon_by_zone:
            carbon_by_zone[site.grid_zone] = await _carbon_curve(
                site.grid_zone, horizon_start, N_SLOTS
            )
        if prices is None:
            prices = price_curve(horizon_start, N_SLOTS, settings.slot_minutes)
        inp = OptimizerInput(
            carbon=carbon_by_zone[site.grid_zone],
            price=prices,
            sessions=[
                SessionInput(
                    session_id=p.session.id,
                    energy_needed_kwh=p.energy_needed_kwh,
                    max_kw=p.max_kw,
                    available=p.available,
                    safety_energy_kwh=SAFETY_ENERGY_KWH,
                )
                for p in to_optimise
            ],
            site_limit_kw=max(0.0, site.site_max_power_kw - manual_kw),
            alpha=alpha,
            beta=beta,
            slot_hours=SLOT_HOURS,
        )
        result, ms = await asyncio.to_thread(_timed_optimize, inp)
        solve_ms += ms
        statuses.append(result.status)
        for p in to_optimise:
            p.schedule_kw = [float(v) for v in result.schedule[p.session.id]]
            p.unmet_kwh = float(result.unmet_energy_kwh.get(p.session.id, 0.0))

    if not statuses:
        return STATUS_SKIPPED, solve_ms
    worst = max(statuses, key=lambda st: _STATUS_SEVERITY.get(st, len(_STATUS_SEVERITY)))
    return worst, solve_ms


async def _send_profiles(plans: list[_Plan]) -> tuple[int, int]:
    """SetChargingProfile to every session's charge point, concurrently: slot 0 of its plan, or
    its manual limit. Returns (accepted, attempted). Offline charge points are skipped."""
    sends: list[tuple[_Plan, Any, int]] = []
    skipped: list[str] = []
    for plan in plans:
        s = plan.session
        # Re-read the manual limit: an override may have arrived while this tick was planning.
        manual_w = registry.manual_limits_w.get(s.id)
        if manual_w is None:
            manual_w = plan.manual_limit_w
        kw = plan.schedule_kw[0] if manual_w is None else float(manual_w) / W_PER_KW
        if not (math.isfinite(kw) and kw >= 0):
            skipped.append(f"session {s.id} on {s.ocpp_id} (invalid limit {kw!r} kW)")
            continue
        limit_w = _to_watts(kw)
        conn = registry.get(s.ocpp_id)
        if conn is None or s.ocpp_transaction_id is None:
            why = "not connected" if conn is None else "no transaction id"
            skipped.append(f"session {s.id} on {s.ocpp_id} ({why}, {limit_w} W)")
            continue
        sends.append((plan, conn, limit_w))
    if skipped:
        logger.warning("SetChargingProfile skipped: %s", "; ".join(skipped))

    results = await asyncio.gather(
        *(
            conn.cp.set_charging_profile(plan.session.ocpp_transaction_id, limit_w)
            for plan, conn, limit_w in sends
        ),
        return_exceptions=True,
    )
    accepted = 0
    failed: list[str] = []
    for (plan, _, limit_w), result in zip(sends, results):
        s = plan.session
        if isinstance(result, BaseException):
            failed.append(f"session {s.id} on {s.ocpp_id} ({limit_w} W): {result!r}")
            continue
        status = str(getattr(result, "value", result))
        if status == ChargingProfileStatus.accepted:
            accepted += 1
        else:
            failed.append(f"session {s.id} on {s.ocpp_id} ({limit_w} W): {status}")
    if failed:
        logger.warning("SetChargingProfile not accepted: %s", "; ".join(failed))
    return accepted, len(sends)


def _tick_record(
    now: datetime,
    reason: str,
    n_sessions: int,
    status: str,
    solve_ms: float,
    unmet_kwh: dict[int, float],
) -> dict:
    return {
        "computed_at": now,
        "reason": reason,
        "n_sessions": n_sessions,
        "status": status,
        "solve_ms": round(solve_ms, 1),
        "unmet_kwh": unmet_kwh,
    }


async def _run_tick(reason: str) -> dict:
    """The tick body; the caller holds ``_tick_lock``. Never raises (cancellation aside)."""
    global _loop
    _loop = asyncio.get_running_loop()
    generation = _generation
    alpha, beta = state.alpha, state.beta
    now = clock.now()
    horizon_start = floor_to_slot(now)
    n_sessions = 0
    solve_ms = 0.0
    try:
        # 1-5: load, derive the inputs, optimise.
        sessions = await asyncio.to_thread(_db_load_active_sessions)
        n_sessions = len(sessions)
        manual_limits = dict(registry.manual_limits_w)
        plans = [_plan_session(s, now, horizon_start, manual_limits.get(s.id)) for s in sessions]
        status, solve_ms = await _optimise_sites(plans, horizon_start, alpha, beta)

        if generation != _generation:
            return _abandoned(now, reason, n_sessions, solve_ms)

        # 6: persist every session's plan (a new version; history is kept). A failure is
        # logged and the plan is still executed: control matters more than history.
        slot_starts = [horizon_start + t * SLOT for t in range(N_SLOTS)]
        rows = [
            {
                "session_id": p.session.id,
                "computed_at": now,
                "slot_start": slot_starts[t],
                "power_kw": p.schedule_kw[t],
            }
            for p in plans
            for t in range(N_SLOTS)
        ]
        try:
            await asyncio.to_thread(_db_insert_schedules, rows)
        except Exception:
            logger.exception(
                "Tick (%s): could not persist the schedules; executing the plan anyway", reason
            )

        if generation != _generation:
            return _abandoned(now, reason, n_sessions, solve_ms)

        unmet = {p.session.id: p.unmet_kwh for p in plans}
        record = _tick_record(now, reason, n_sessions, status, solve_ms, unmet)
        latest = dict(state.latest_schedules)
        for p in plans:
            latest[p.session.id] = {
                "computed_at": now,
                "slots": list(zip(slot_starts, p.schedule_kw)),
            }
        state.latest_schedules = latest
        state.last_tick = record

        # 7: execute slot 0 only.
        accepted, attempted = await _send_profiles(plans)

        # 8: one INFO line per tick.
        n_manual = sum(1 for p in plans if p.manual_limit_w is not None)
        n_optimised = sum(1 for p in plans if p.optimise)
        logger.info(
            "Tick (%s) at %s: %d active session(s) [%d optimised, %d manual], status %s, "
            "solve %.1f ms, slot 0 = %.1f kW planned, profiles accepted %d/%d",
            reason, now.isoformat(timespec="seconds"), n_sessions, n_optimised, n_manual,
            status, solve_ms, math.fsum(p.schedule_kw[0] for p in plans), accepted, attempted,
        )
        return record
    except Exception:
        logger.exception(
            "Tick (%s) failed; the charge points keep their last limits", reason
        )
        return _tick_record(now, reason, n_sessions, STATUS_ERROR, solve_ms, {})


def _abandoned(now: datetime, reason: str, n_sessions: int, solve_ms: float) -> dict:
    logger.info(
        "Tick (%s) at %s abandoned: the orchestrator state was reset while it ran",
        reason, now.isoformat(timespec="seconds"),
    )
    return _tick_record(now, reason, n_sessions, STATUS_ABANDONED, solve_ms, {})


async def tick(reason: str = REASON_SCHEDULED) -> dict:
    """Run one tick now (after any tick in progress) and return its record.

    Never raises: failures are logged and reported as status "error". Do not await this from
    an OCPP ``@on`` handler (DEADLOCK RULE); use ``request_tick()`` there.
    """
    async with _tick_lock:
        return await _run_tick(reason)


def request_tick(reason: str) -> None:
    """Ask for a tick soon without waiting for it (fire-and-forget).

    Requests coalesce: while a requested tick is queued (it has not taken the lock yet), later
    requests only add their reason to it. Once it starts, a new request queues a new tick, so
    every change is seen by some tick that starts after it. Safe to call from any thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _loop
        if loop is None or loop.is_closed():
            logger.warning("Tick request %r ignored: the orchestrator loop is not running", reason)
            return
        loop.call_soon_threadsafe(_request_tick_on_loop, reason)
        return
    _request_tick_on_loop(reason)


def _request_tick_on_loop(reason: str) -> None:
    global _queued
    if _queued is not None:
        if reason not in _queued.reasons:
            _queued.reasons.append(reason)
        return
    request = _TickRequest(reason)
    _queued = request
    _spawn(_run_requested_tick(request), f"orchestrator-tick:{reason}")


async def _run_requested_tick(request: _TickRequest) -> None:
    global _queued
    try:
        async with _tick_lock:
            if _queued is request:
                _queued = None  # from now on a new request queues another tick
            await _run_tick("+".join(request.reasons))
    finally:
        if _queued is request:  # cancelled before it took the lock
            _queued = None


def _task_done(task: asyncio.Task) -> None:
    """Drop the task's strong reference and log a failure (asyncio would only report it at
    garbage-collection time, long after the fact)."""
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background task %s failed", task.get_name(), exc_info=task.exception())


def _spawn(coro: Any, name: str) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_task_done)
    return task


# --------------------------------------------------------------------------------------------
# Session start: the baseline shadow simulation
# --------------------------------------------------------------------------------------------


async def on_session_started(session_id: int) -> None:
    """Compute and store the session's baseline (co2_baseline_g, cost_baseline_inr), then
    request a tick. Never raises.

    The baseline is the spec's naive "full acceptance rate from plug-in" simulation
    (``baseline.simulate_baseline``, which never sends OCPP), priced with the forecast carbon
    intensity and the tariff on 15-minute slots starting at floor_to_slot(plugged_in_at).
    The tick is requested even when the baseline fails, so the new car is always planned.
    """
    global _loop
    _loop = asyncio.get_running_loop()
    generation = _generation
    try:
        loaded = await asyncio.to_thread(_db_load_session_for_baseline, session_id)
        if loaded is None:
            logger.warning("Session %s not found; no baseline computed", session_id)
        else:
            session, zone = loaded
            start = floor_to_slot(session.plugged_in_at)
            carbon = await _carbon_curve(zone, start, N_SLOTS)
            prices = price_curve(start, N_SLOTS, settings.slot_minutes)
            co2_g, cost_inr = await asyncio.to_thread(simulate_baseline, session, carbon, prices)
            if generation != _generation:
                logger.info(
                    "Baseline of session %s dropped: the orchestrator state was reset", session_id
                )
            elif await asyncio.to_thread(
                _db_store_baseline, session_id, session.plugged_in_at, co2_g, cost_inr
            ):
                logger.info(
                    "Session %s baseline (naive charging from plug-in): %.0f g CO2, %.2f INR",
                    session_id, co2_g, cost_inr,
                )
            else:
                logger.warning("Session %s is gone; its baseline was not stored", session_id)
    except Exception:
        logger.exception("Could not compute the baseline of session %s", session_id)
    request_tick(REASON_SESSION_START)


# --------------------------------------------------------------------------------------------
# Weights, reset, schedules
# --------------------------------------------------------------------------------------------


def set_weights(alpha: float, beta: float) -> None:
    """Set the optimizer weights used from the next tick on (both finite and >= 0)."""
    for name, value in (("alpha", alpha), ("beta", beta)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    state.alpha = float(alpha)
    state.beta = float(beta)
    logger.info("Optimizer weights set: alpha=%g, beta=%g", state.alpha, state.beta)


def reset_state() -> None:
    """Weights back to the defaults; forget the last tick and every plan held in memory.

    A tick in flight while this runs discards its results ("abandoned").
    """
    global _generation
    _generation += 1
    state.alpha = DEFAULT_ALPHA
    state.beta = DEFAULT_BETA
    state.last_tick = None
    state.latest_schedules = {}
    logger.info("Orchestrator state reset (weights alpha=%g, beta=%g)", state.alpha, state.beta)


def get_latest_schedules() -> dict[int, dict]:
    """session_id -> ``{"computed_at", "slots": [(slot_start, kW)] x 96}``: the newest plan of
    every session that has one -- from ``state`` first, from the database (latest computed_at
    per session) for the rest, e.g. sessions planned before a restart.

    Includes finished sessions' last plans; callers filter by session status. The entries are
    shared, read-only. A database failure is logged and only the in-memory plans are returned.
    Blocking (a DB query): call it from a worker thread or a sync endpoint where possible.
    """
    in_memory = dict(state.latest_schedules)
    try:
        combined = _db_latest_schedules(set(in_memory))
    except Exception:
        logger.exception("Could not load the persisted schedules; returning in-memory plans only")
        combined = {}
    combined.update(in_memory)
    return combined


# --------------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------------


def start_scheduler() -> AsyncIOScheduler:
    """Start the periodic tick: every ``settings.tick_minutes`` SIMULATED minutes, i.e. every
    tick_minutes * 60 / TIME_SCALE real seconds (5 s at the default TIME_SCALE 60).

    Call from the running event loop (the app lifespan). Overlapping runs are prevented by
    ``max_instances=1`` and runs missed while busy are merged by ``coalesce=True``.
    """
    global _loop
    loop = asyncio.get_running_loop()
    _loop = loop
    interval_s = settings.tick_minutes * SECONDS_PER_MINUTE / clock.time_scale
    if not (math.isfinite(interval_s) and interval_s > 0):
        raise ValueError(
            f"tick interval must be > 0 s, got {interval_s!r} "
            f"(tick_minutes={settings.tick_minutes}, time_scale={clock.time_scale})"
        )
    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.add_job(
        tick,
        IntervalTrigger(seconds=interval_s),
        kwargs={"reason": REASON_SCHEDULED},
        id=TICK_JOB_ID,
        name="orchestrator tick",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Orchestrator tick every %g simulated minute(s) = %.3g real s (TIME_SCALE %g)",
        settings.tick_minutes, interval_s, clock.time_scale,
    )
    return scheduler


def stop_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    """Stop the periodic tick (a tick already running finishes on its own). None is a no-op."""
    if scheduler is None:
        return
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Orchestrator scheduler stopped")
