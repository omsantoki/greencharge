"""Demo scenarios and the demo reset (BUILD_SPEC Phase 4 acceptance; Phase 8 one-click buttons).

A scenario is a deterministic script of plug-ins. ``run_scenario(name)``:

1. resets the demo state (``perform_reset()``, below);
2. sets the simulation clock to ``start_local`` (site time, ``settings.site_timezone``) on TODAY's
   date there. "Today" is the real calendar date at the site, not the simulated one: the
   simulated clock runs ``TIME_SCALE`` times faster (a simulated day passes every 24 real minutes
   at the default 60), so its date drifts, while the real date makes repeated runs identical;
3. plugs each car in at ``start + offset_min`` SIMULATED minutes (it waits
   ``offset_min * 60 / TIME_SCALE`` real seconds, measured from the moment the clock was set),
   through ``app.routers.debug.perform_plug_in`` -- the same OCPP round trip as
   POST /api/debug/plug-in -- with ``hours_until_departure`` = the next ``depart_local`` after
   the plug-in time minus the plug-in time.

Progress is kept in memory for GET /api/demo/status (``scenario_status()``). A plug-in that the
charge point refuses with 409 is retried for a few real seconds (a charge point that has just
been stopped by the reset accepts a new car only once it is back to Available); any other
failure, or a 409 that persists, stops the scenario with ``error`` set. Phase 4 defines only
"evening_rush"; Phase 8 adds the other scenarios.

Demo reset (POST /api/demo/reset, and step 1 of every scenario), ``perform_reset()``:

1. drops every pending plug-in (``registry.pending_plugins``) and fails the futures in
   ``registry.session_waiters`` with ``PlugInError`` 409, so no new session can start;
2. sends RemoteStopTransaction to every connected charge point with an active session, all at
   once, and waits for the StopTransactions (and for those charge points to report Available
   again), at most ``RESET_STOP_TIMEOUT_S`` = 3 s in total;
3. ``TRUNCATE meter_values, schedules, sessions RESTART IDENTITY CASCADE`` -- sites, chargers and
   grid_data (the seed data and the carbon cache) survive;
4. clears ``registry.manual_limits_w``, ``pending_plugins`` and ``session_waiters``;
5. waits for any orchestrator tick still in flight (it may have read sessions from before the
   truncate) by running one tick on the now empty database -- for at most ``RESET_STOP_TIMEOUT_S``,
   so an unresponsive charge point cannot hold the reset up -- then ``loop.reset_state()`` (weights
   back to the defaults, no last tick, no latest schedules).

The endpoint's reset (``reset_demo()``) first cancels a scenario that is still running.

Everything here runs on the event loop the CSMS runs on (the OCPP registry and its futures live
there); database work runs in worker threads.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from typing import Any
from zoneinfo import ZoneInfo

from ocpp.v16.enums import ChargePointStatus, RemoteStartStopStatus
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.clock import clock
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Charger, MeterValue, Schedule, Session
from app.ocpp import registry
from app.ocpp.handlers import STATUS_TIMEOUT
from app.orchestrator import loop as orchestrator
from app.routers.debug import PlugInError, perform_plug_in
from app.schemas import PlugInRequest

logger = logging.getLogger("greencharge.scenarios")

# --------------------------------------------------------------------------------------------
# Scenario definitions
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlugInEvent:
    """One car plugging in, ``offset_min`` simulated minutes after the scenario start."""

    offset_min: int
    charger_id: int
    vehicle_model: str  # a model in data/vehicles.json
    soc_start: float  # 0.0-1.0
    soc_target: float  # 0.0-1.0


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    start_local: str  # "HH:MM", site-local time the simulation clock is set to
    depart_local: str  # "HH:MM", site-local time every car leaves (the next one after plug-in)
    events: tuple[PlugInEvent, ...]


SCENARIOS: dict[str, Scenario] = {
    "evening_rush": Scenario(
        name="evening_rush",
        description="6 cars plug in 18:30-19:15, all leave 07:00. The headline scenario.",
        start_local="18:30",
        depart_local="07:00",
        # The six data/vehicles.json models in file order on chargers 1..6, one every 9 simulated
        # minutes (18:30-19:15). Every target is <= 0.80, so the taper never hides the site limit.
        events=(
            PlugInEvent(0, 1, "Tata Nexon EV", 0.30, 0.80),
            PlugInEvent(9, 2, "Tata Tiago EV", 0.25, 0.80),
            PlugInEvent(18, 3, "MG ZS EV", 0.35, 0.80),
            PlugInEvent(27, 4, "Mahindra XUV400", 0.20, 0.80),
            PlugInEvent(36, 5, "BYD Atto 3", 0.40, 0.80),
            PlugInEvent(45, 6, "Hyundai Kona", 0.30, 0.80),
        ),
    ),
}

# --------------------------------------------------------------------------------------------
# Tunables (real seconds unless stated)
# --------------------------------------------------------------------------------------------

RESET_STOP_TIMEOUT_S = 3.0  # contract 6b: RemoteStop + waiting for the StopTransactions, in total
RESET_POLL_INTERVAL_S = 0.1  # how often the reset looks at the sessions it asked to stop
RESET_TICK_REASON = "demo_reset"

# TRUNCATE needs an exclusive lock on the three tables. It gives up after this long instead of
# queueing indefinitely behind a transaction that is itself waiting for the event loop, and is
# then retried (lock timeout or deadlock only).
TRUNCATE_LOCK_TIMEOUT_MS = 2000
TRUNCATE_ATTEMPTS = 3
TRUNCATE_RETRY_DELAY_S = 0.2
_RETRYABLE_PGCODES = frozenset({"55P03", "40P01"})  # lock_not_available, deadlock_detected

# A plug-in refused with 409 (charge point still finishing, briefly disconnected) is retried.
PLUG_IN_RETRY_WINDOW_S = 3.0
PLUG_IN_RETRY_DELAY_S = 0.5

SESSION_ACTIVE = "active"  # Session.status of a session in progress
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

# Truncated in this order (children first); CASCADE covers anything else referencing sessions.
_RESET_TABLES = (MeterValue.__tablename__, Schedule.__tablename__, Session.__tablename__)

# Status strings of the per-session reset report besides the charge point's answer.
STOP_NOT_CONNECTED = "NotConnected"
STOP_NO_TRANSACTION = "NoTransaction"

# Event statuses in GET /api/demo/status.
EVENT_PENDING = "pending"
EVENT_PLUGGED_IN = "plugged_in"
EVENT_FAILED = "failed"
EVENT_SKIPPED = "skipped"


class UnknownScenarioError(KeyError):
    """No scenario with that name (HTTP 404)."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "unknown scenario"


class ScenarioBusyError(RuntimeError):
    """A scenario is already running, or a demo reset is in progress (HTTP 409)."""


def get_scenario(name: str) -> Scenario:
    scenario = SCENARIOS.get(name)
    if scenario is None:
        raise UnknownScenarioError(
            f"Unknown scenario {name!r}; known scenarios: {', '.join(SCENARIOS)}"
        )
    return scenario


# --------------------------------------------------------------------------------------------
# Site-local time helpers
# --------------------------------------------------------------------------------------------


def _site_tz() -> ZoneInfo:
    return ZoneInfo(settings.site_timezone)


def _local_time(hhmm: str) -> time:
    """"HH:MM" -> time (ValueError for anything else)."""
    return time.fromisoformat(hhmm)


def scenario_start(scenario: Scenario, day: date | None = None) -> datetime:
    """The instant the scenario starts: ``start_local`` on ``day`` (default: today's real date at
    the site) in the site time zone, as aware UTC."""
    tz = _site_tz()
    if day is None:
        day = datetime.now(tz).date()  # the REAL date at the site (see the module docstring)
    return datetime.combine(day, _local_time(scenario.start_local), tzinfo=tz).astimezone(
        timezone.utc
    )


def hours_until_next(hhmm: str, after: datetime) -> float:
    """Hours from ``after`` to the next site-local ``hhmm`` strictly after it."""
    tz = _site_tz()
    local = after.astimezone(tz)
    at = _local_time(hhmm)
    candidate = datetime.combine(local.date(), at, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), at, tzinfo=tz)
    # Subtract in UTC: aware datetimes sharing one tzinfo would subtract as wall-clock times.
    delta = candidate.astimezone(timezone.utc) - after.astimezone(timezone.utc)
    return delta.total_seconds() / SECONDS_PER_HOUR


def _local_hhmm(ts: datetime) -> str:
    return ts.astimezone(_site_tz()).strftime("%H:%M")


# --------------------------------------------------------------------------------------------
# Demo reset
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ActiveSession:
    session_id: int
    transaction_id: int | None
    charger_id: int
    ocpp_id: str


_reset_lock = asyncio.Lock()  # one reset at a time (endpoint and scenarios)


def _db_active_sessions() -> list[_ActiveSession]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Session.id, Session.ocpp_transaction_id, Charger.id, Charger.ocpp_id)
            .join(Session.charger)
            .where(Session.status == SESSION_ACTIVE)
            .order_by(Session.id)
        ).all()
    return [_ActiveSession(*row) for row in rows]


def _db_stop_progress(
    session_ids: list[int], charger_ids: list[int]
) -> tuple[set[int], set[int]]:
    """(those sessions still active, those chargers not reporting Available)."""
    with SessionLocal() as db:
        still_active = set(
            db.scalars(
                select(Session.id).where(
                    Session.id.in_(session_ids), Session.status == SESSION_ACTIVE
                )
            )
        )
        not_available = set(
            db.scalars(
                select(Charger.id).where(
                    Charger.id.in_(charger_ids),
                    Charger.status != ChargePointStatus.available.value,
                )
            )
        )
    return still_active, not_available


def _db_truncate() -> None:
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('lock_timeout', :value, true)"),
            {"value": f"{TRUNCATE_LOCK_TIMEOUT_MS}ms"},
        )
        conn.execute(text(f"TRUNCATE TABLE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE"))


async def _truncate() -> None:
    for attempt in range(1, TRUNCATE_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_db_truncate)
            return
        except OperationalError as exc:
            pgcode = getattr(exc.orig, "pgcode", None)
            if pgcode not in _RETRYABLE_PGCODES or attempt == TRUNCATE_ATTEMPTS:
                raise
            logger.warning(
                "Demo reset: TRUNCATE attempt %d/%d could not get its lock (%s); retrying",
                attempt, TRUNCATE_ATTEMPTS, pgcode,
            )
            await asyncio.sleep(TRUNCATE_RETRY_DELAY_S)


def _mark_retrieved(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()  # nobody may await a waiter we failed; avoid "never retrieved" noise


def _abort_pending_plug_ins() -> None:
    """Forget pending plug-ins and fail their waiters, so no StartTransaction can create a
    session any more (it finds no pending parameters and is answered Invalid)."""
    registry.pending_plugins.clear()
    waiters = list(registry.session_waiters.values())
    registry.session_waiters.clear()
    for waiter in waiters:
        if not waiter.done():
            waiter.set_exception(
                PlugInError(HTTPStatus.CONFLICT, "The plug-in was cut short by a demo reset")
            )
            waiter.add_done_callback(_mark_retrieved)


async def _remote_stop(active: _ActiveSession, deadline: float) -> str:
    """RemoteStopTransaction for one session; the charge point's status or why none was sent."""
    conn = registry.get(active.ocpp_id)
    if conn is None:
        return STOP_NOT_CONNECTED
    if active.transaction_id is None:
        return STOP_NO_TRANSACTION
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        return await asyncio.wait_for(conn.cp.remote_stop(active.transaction_id), remaining)
    except TimeoutError:
        return STATUS_TIMEOUT


async def _wait_for_stops(
    stopping: list[_ActiveSession], deadline: float
) -> tuple[set[int], set[int]]:
    """Wait until the sessions are no longer active and their chargers report Available again
    (the simulated charge point takes a new car only then), or until ``deadline``.
    Returns what is still outstanding: (active session ids, charger ids not Available)."""
    if not stopping:
        return set(), set()
    loop = asyncio.get_running_loop()
    session_ids = [a.session_id for a in stopping]
    charger_ids = sorted({a.charger_id for a in stopping})
    while True:
        still_active, not_available = await asyncio.to_thread(
            _db_stop_progress, session_ids, charger_ids
        )
        remaining = deadline - loop.time()
        if (not still_active and not not_available) or remaining <= 0:
            return still_active, not_available
        await asyncio.sleep(min(RESET_POLL_INTERVAL_S, remaining))


async def perform_reset() -> dict[str, Any]:
    """Reset all demo state (see the module docstring). Seed data and grid_data survive.

    Returns ``{"reset": true, "sessions": [{"session_id", "ocpp_id", "remote_stop",
    "stopped"}], "waited_s", "truncated"}``: every session that was active, what its charge
    point answered to RemoteStopTransaction ("NotConnected" when it was not connected), and
    whether its StopTransaction arrived in time. Database errors propagate.
    """
    async with _reset_lock:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + RESET_STOP_TIMEOUT_S

        _abort_pending_plug_ins()
        active = await asyncio.to_thread(_db_active_sessions)
        answers = await asyncio.gather(*(_remote_stop(a, deadline) for a in active))
        stopping = [
            a for a, answer in zip(active, answers) if answer == RemoteStartStopStatus.accepted
        ]
        still_active, not_available = await _wait_for_stops(stopping, deadline)
        waited_s = loop.time() - started
        if still_active or not_available:
            logger.warning(
                "Demo reset: after %.1f s sessions %s are still active and chargers %s not "
                "Available; truncating anyway",
                waited_s, sorted(still_active), sorted(not_available),
            )

        await _truncate()
        registry.manual_limits_w.clear()
        _abort_pending_plug_ins()
        # A tick that read the old sessions may still be running; tick() holds the orchestrator's
        # lock, so this one runs only after it and sees the empty tables. Then forget its state.
        # The wait is bounded: a charge point that stops answering can hold the tick lock for the
        # OCPP response timeout, and the reset must never hang on it. Giving up is safe --
        # reset_state() makes the tick in flight discard its results ("abandoned").
        try:
            await asyncio.wait_for(orchestrator.tick(RESET_TICK_REASON), RESET_STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Demo reset: an orchestrator tick was still running after %.1f s; resetting the "
                "orchestrator state anyway (its results will be discarded)", RESET_STOP_TIMEOUT_S,
            )
        orchestrator.reset_state()

    logger.info(
        "Demo reset: %d active session(s), %d stopped by RemoteStopTransaction in %.1f s; "
        "tables %s truncated",
        len(active), sum(1 for a in stopping if a.session_id not in still_active), waited_s,
        ", ".join(_RESET_TABLES),
    )
    return {
        "reset": True,
        "sessions": [
            {
                "session_id": a.session_id,
                "ocpp_id": a.ocpp_id,
                "remote_stop": str(answer),
                "stopped": answer == RemoteStartStopStatus.accepted
                and a.session_id not in still_active,
            }
            for a, answer in zip(active, answers)
        ],
        "waited_s": round(waited_s, 3),
        "truncated": list(_RESET_TABLES),
    }


# --------------------------------------------------------------------------------------------
# Running a scenario
# --------------------------------------------------------------------------------------------


@dataclass
class _ScenarioRun:
    scenario: Scenario
    events: list[dict[str, Any]]
    step: int = 0
    running: bool = True
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.name,
            "running": self.running,
            "step": self.step,
            "total_steps": len(self.scenario.events),
            "events": [dict(event) for event in self.events],
            "error": self.error,
        }


_TASK_NAME_PREFIX = "scenario:"
_run: _ScenarioRun | None = None  # the latest run (running or finished)
_task: asyncio.Task | None = None  # the background task started by start_scenario()
_resets_in_progress = 0  # reset_demo() calls under way; start_scenario() refuses meanwhile


def _task_alive() -> bool:
    return _task is not None and not _task.done()


def is_running() -> bool:
    """True while a scenario runs (or its background task has been created but not started)."""
    return _task_alive() or (_run is not None and _run.running)


def scenario_status() -> dict[str, Any]:
    """GET /api/demo/status: ``{"scenario", "running", "step", "total_steps", "events",
    "error"}``. ``step`` counts the plug-ins done; each event records its schedule and, once
    done, the session it created."""
    if _run is None:
        return {
            "scenario": None,
            "running": False,
            "step": 0,
            "total_steps": 0,
            "events": [],
            "error": None,
        }
    status = _run.snapshot()
    status["running"] = is_running()
    return status


def _new_run(scenario: Scenario, start: datetime) -> _ScenarioRun:
    events = []
    for index, event in enumerate(scenario.events, start=1):
        scheduled = start + timedelta(minutes=event.offset_min)
        events.append(
            {
                "step": index,
                "offset_min": event.offset_min,
                "scheduled_at": scheduled.isoformat(),
                "local_time": _local_hhmm(scheduled),
                "charger_id": event.charger_id,
                "vehicle_model": event.vehicle_model,
                "soc_start": event.soc_start,
                "soc_target": event.soc_target,
                "status": EVENT_PENDING,
                "requested_at": None,
                "hours_until_departure": None,
                "session_id": None,
                "transaction_id": None,
                "ocpp_id": None,
                "detail": None,
            }
        )
    return _ScenarioRun(scenario=scenario, events=events)


async def _plug_in(event: PlugInEvent, depart_local: str, record: dict[str, Any]) -> dict:
    """perform_plug_in for one event, retrying a 409 for up to PLUG_IN_RETRY_WINDOW_S. The
    departure is recomputed for every attempt, so it always lands on ``depart_local``."""
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + PLUG_IN_RETRY_WINDOW_S
    while True:
        requested_at = clock.now()
        hours = hours_until_next(depart_local, requested_at)
        record["requested_at"] = requested_at.isoformat()
        record["hours_until_departure"] = hours
        request = PlugInRequest(
            charger_id=event.charger_id,
            vehicle_model=event.vehicle_model,
            soc_start=event.soc_start,
            soc_target=event.soc_target,
            hours_until_departure=hours,
        )
        try:
            return await perform_plug_in(request)
        except PlugInError as exc:
            if exc.status_code != HTTPStatus.CONFLICT or (
                loop.time() + PLUG_IN_RETRY_DELAY_S > give_up_at
            ):
                raise
            logger.info(
                "Scenario plug-in on charger %d refused (%s); retrying in %g s",
                event.charger_id, exc.detail, PLUG_IN_RETRY_DELAY_S,
            )
            await asyncio.sleep(PLUG_IN_RETRY_DELAY_S)


def describe_error(exc: BaseException) -> str:
    """A one-line description of an unexpected failure for ``error``. SQLAlchemy errors are
    reduced to the database driver's message."""
    if isinstance(exc, SQLAlchemyError) and getattr(exc, "orig", None) is not None:
        message = " ".join(str(exc.orig).split())
        return f"Database error ({type(exc.orig).__name__}): {message}"
    return f"{type(exc).__name__}: {exc}"


def _skip_pending(run: _ScenarioRun) -> None:
    for record in run.events:
        if record["status"] == EVENT_PENDING:
            record["status"] = EVENT_SKIPPED


async def _execute(run: _ScenarioRun, start: datetime) -> None:
    scenario = run.scenario
    total = len(scenario.events)

    try:
        await perform_reset()
    except Exception as exc:
        run.error = f"Demo reset failed: {describe_error(exc)}"
        logger.exception("Scenario %s: demo reset failed", scenario.name)
        return
    clock.set_now(start)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    logger.info(
        "Scenario %s: clock set to %s (%s site time), %d plug-in(s)",
        scenario.name, start.isoformat(), scenario.start_local, total,
    )

    for index, (event, record) in enumerate(zip(scenario.events, run.events), start=1):
        due = t0 + clock.sim_seconds_to_real(event.offset_min * SECONDS_PER_MINUTE)
        delay = due - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            result = await _plug_in(event, scenario.depart_local, record)
        except PlugInError as exc:
            record["status"] = EVENT_FAILED
            record["detail"] = f"HTTP {int(exc.status_code)}: {exc.detail}"
            run.error = (
                f"Step {index}/{total} (charger {event.charger_id}, {event.vehicle_model}) "
                f"failed: HTTP {int(exc.status_code)}: {exc.detail}"
            )
            logger.error("Scenario %s: %s", scenario.name, run.error)
            return
        record.update(
            status=EVENT_PLUGGED_IN,
            session_id=result["session_id"],
            transaction_id=result["transaction_id"],
            ocpp_id=result["ocpp_id"],
        )
        run.step = index
        logger.info(
            "Scenario %s: step %d/%d, %s on %s -> session %s, departs in %.2f h",
            scenario.name, index, total, event.vehicle_model, result["ocpp_id"],
            result["session_id"], record["hours_until_departure"],
        )


async def run_scenario(name: str) -> dict[str, Any]:
    """Run scenario ``name`` to the end: reset -> set the clock -> timed plug-ins (see the module
    docstring). Records progress for ``scenario_status()`` and returns the final status.

    Raises ``UnknownScenarioError`` for an unknown name and ``ScenarioBusyError`` when another
    scenario is running. Failures during the run are recorded in ``error``, not raised.
    """
    global _run
    scenario = get_scenario(name)
    other_task = _task_alive() and _task is not asyncio.current_task()
    if other_task or (_run is not None and _run.running):
        running = _run.scenario.name if _run is not None else name
        raise ScenarioBusyError(f"Scenario {running!r} is already running")

    start = scenario_start(scenario)
    run = _new_run(scenario, start)
    _run = run
    try:
        await _execute(run, start)
    except asyncio.CancelledError:
        run.error = run.error or "Scenario cancelled"
        logger.warning("Scenario %s: %s", scenario.name, run.error)
        raise
    except Exception as exc:
        run.error = describe_error(exc)
        logger.exception("Scenario %s failed", scenario.name)
    finally:
        run.running = False
        if run.error is not None:
            _skip_pending(run)
    if run.error is None:
        logger.info("Scenario %s: all %d plug-ins done", scenario.name, run.step)
    return run.snapshot()


def start_scenario(name: str) -> Scenario:
    """Start ``run_scenario(name)`` in a background task and return the scenario.

    Raises ``UnknownScenarioError`` (unknown name) or ``ScenarioBusyError`` (a scenario is
    running or a demo reset is in progress). Must be called on the event loop.
    """
    global _task
    scenario = get_scenario(name)
    if is_running():
        current = _run.scenario.name if _run is not None else name
        step = f" (step {_run.step}/{len(_run.scenario.events)})" if _run is not None else ""
        raise ScenarioBusyError(f"Scenario {current!r} is already running{step}")
    if _resets_in_progress:
        raise ScenarioBusyError("A demo reset is in progress")
    _task = asyncio.create_task(run_scenario(name), name=f"{_TASK_NAME_PREFIX}{name}")
    return scenario


async def cancel_scenario(reason: str) -> str | None:
    """Cancel the running background scenario and wait for it to end; returns its name, or None
    when none was running."""
    task = _task
    if task is None or task.done():
        return None
    name = task.get_name().removeprefix(_TASK_NAME_PREFIX)
    if _run is not None and _run.running and _run.error is None:
        _run.error = reason
    task.cancel()
    await asyncio.wait({task})
    return name


async def reset_demo() -> dict[str, Any]:
    """POST /api/demo/reset: cancel a running scenario, then ``perform_reset()``.
    The result adds ``"cancelled_scenario"`` (its name, or null)."""
    global _resets_in_progress
    _resets_in_progress += 1
    try:
        cancelled = await cancel_scenario("Scenario cancelled by POST /api/demo/reset")
        result = await perform_reset()
    finally:
        _resets_in_progress -= 1
    result["cancelled_scenario"] = cancelled
    return result
