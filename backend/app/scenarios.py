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
   the plug-in time;
4. runs the scenario's ``fault``, when it has one (only "fault_injection" has): at
   ``fault.offset_min`` SIMULATED minutes after the clock was set -- always after the last
   plug-in -- it sends that charger's charge point a DataTransfer (vendor "GreenCharge", message
   "SimFault"). The simulated charge point drops its power to 0 kW and reports
   StatusNotification(Faulted); the CSMS stores that status and asks the orchestrator for a
   re-plan, and a faulted charger is unavailable in every slot, so its session gets an all-zero
   plan and the others are re-optimised with the headroom it leaves. The step waits for the
   Faulted status to arrive, so the scenario is only done once the fault is real.

Progress is kept in memory for GET /api/demo/status (``scenario_status()``). A plug-in that the
charge point refuses with 409 is retried for a few real seconds (a charge point that has just
been stopped by the reset accepts a new car only once it is back to Available); any other
failure, or a 409 that persists, stops the scenario with ``error`` set; so does a fault that
could not be delivered (the scenario IS the fault). The fault has its own entry in the status
(``"fault"``), not a step of its own. Phase 4 defines "evening_rush"; Phase 8 adds
"workplace_solar", "tight_deadline" and "fault_injection".

Demo reset (POST /api/demo/reset, and step 1 of every scenario), ``perform_reset()``:

1. drops every pending plug-in (``registry.pending_plugins``) and fails the futures in
   ``registry.session_waiters`` with ``PlugInError`` 409, so no new session can start;
2. works out which charge points have to come back to Available from BOTH sides -- the active
   sessions in the database, AND the charge points the CSMS can see are still transacting
   without one: a connector it last saw in any state but Available, or a live transaction id in
   ``registry.live_transactions``. That second group is what a backend restart leaves behind:
   the charge points keep their transaction across it (a real one does too, since OCPP has no
   call that makes one forget) while the backend that knew those sessions is gone, so a
   reset that reconciled only with the database found nothing to stop, truncated the tables and
   left every connector refusing the next plug-in -- for good, since no later reset could see
   them either;
3. sends each of them one RemoteStopTransaction and waits for the StopTransactions (and for those
   charge points to report Available again), at most ``RESET_STOP_TIMEOUT_S`` = 3 s in total. The
   wait keeps re-trying the two cases where nothing could be sent yet: a charge point that has
   not reconnected, and a transaction whose id is not known yet (it arrives with that charge
   point's next MeterValues). That is what makes a reset right after a restart work -- the charge
   points are back within a second of the CSMS listening again, and a reset that raced them by
   milliseconds is what wedged the demo;
4. ``TRUNCATE meter_values, schedules, sessions RESTART IDENTITY CASCADE`` -- sites, chargers and
   grid_data (the seed data and the carbon cache) survive;
5. clears ``registry.manual_limits_w``, ``pending_plugins`` and ``session_waiters``, and keeps in
   ``live_transactions`` the transaction id of every charge point that did NOT come back: with
   the tables gone that id is the only thing a later reset could stop it with;
6. puts the simulation clock back to ``reset_start()`` -- 18:30 site time on today's real date,
   where the headline scenario starts -- so that a reset ends at the same instant every time and
   the demo state a presenter finds after it is the same every time (see ``reset_start``);
7. waits for any orchestrator tick still in flight (it may have read sessions from before the
   truncate) by running one tick on the now empty database -- for at most ``RESET_STOP_TIMEOUT_S``,
   so an unresponsive charge point cannot hold the reset up -- then ``loop.reset_state()`` (weights
   back to the defaults, no last tick, no latest schedules).

The endpoint's reset (``reset_demo()``) first cancels a scenario that is still running. A reset
that could not bring every charge point back says so: ``"reset": false``, with the reason on the
charge point it failed on. The tables are cleared either way, but a charge point still holding a
transaction refuses the next plug-in, and the operator dashboard has to see that instead of a
green line it would believe.

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

from ocpp.v16 import call
from ocpp.v16.enums import ChargePointStatus, DataTransferStatus
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.clock import clock
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Charger, MeterValue, Schedule, Session
from app.ocpp import registry
from app.ocpp.handlers import PLUG_IN_VENDOR_ID, STATUS_CALL_ERROR, STATUS_TIMEOUT
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
class FaultEvent:
    """One charger going Faulted, ``offset_min`` simulated minutes after the scenario start.

    Keep the offset after the last plug-in: the step runs once every car is in.
    """

    offset_min: int
    charger_id: int


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    start_local: str  # "HH:MM", site-local time the simulation clock is set to
    depart_local: str  # "HH:MM", site-local time every car leaves (the next one after plug-in)
    events: tuple[PlugInEvent, ...]
    fault: FaultEvent | None = None  # the fault injected after the plug-ins, if any


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
    "workplace_solar": Scenario(
        name="workplace_solar",
        description="6 cars plug in 09:00-09:30, leave 18:00. Shows the midday solar capture.",
        start_local="09:00",
        depart_local="18:00",
        # Office hours: nine hours of slack for about 73 kWh (grid side), which the site's 40 kW
        # could deliver in under two. The cheapest four hours of the day by carbon intensity are
        # 11:00-15:00 (520 gCO2/kWh against 620 before and 650 after, and the solar tariff block
        # 09:00-17:00 is flat), so the whole fleet is planned into that trough -- the midday
        # cluster on the Gantt is the point of this scenario. Every car keeps room to move: none
        # needs more than about two hours at its own AC limit.
        events=(
            PlugInEvent(0, 1, "Tata Nexon EV", 0.45, 0.70),
            PlugInEvent(6, 2, "Tata Tiago EV", 0.40, 0.75),
            PlugInEvent(12, 3, "MG ZS EV", 0.50, 0.75),
            PlugInEvent(18, 4, "Mahindra XUV400", 0.45, 0.75),
            PlugInEvent(24, 5, "BYD Atto 3", 0.50, 0.70),
            PlugInEvent(30, 6, "Hyundai Kona", 0.40, 0.70),
        ),
    ),
    "tight_deadline": Scenario(
        name="tight_deadline",
        description="1 car needs 45 kWh in 3 hours. Demonstrates the relaxed/infeasible path.",
        start_local="19:00",
        depart_local="22:00",
        # 0.75 x 60.5 kWh = 45.4 kWh wanted; the BYD accepts 7.0 kW AC, so three hours can deliver
        # 7.0 x 3 x 0.92 = 19.3 kWh at best, whatever the charger and the site allow. (C1) cannot
        # hold, the LP is re-solved with it soft, and the tick reports "relaxed" with about
        # 26 kWh unmet -- the honest answer this scenario exists to show.
        events=(PlugInEvent(0, 1, "BYD Atto 3", 0.15, 0.90),),
    ),
    "fault_injection": Scenario(
        name="fault_injection",
        description="Mid-session, CP003 goes Faulted. Shows rebalancing.",
        start_local="18:30",
        depart_local="07:00",
        # The evening rush again, arriving three simulated minutes apart so the fault comes soon
        # after the last car. The six cars want 46.8 kW together and the site allows 40, so the
        # plan is site-limited; CP003 (the 11 kW MG ZS EV) going Faulted at 18:55 leaves 35.8 kW
        # of demand under the same 40 kW, and the next tick gives the five others what it freed.
        events=(
            PlugInEvent(0, 1, "Tata Nexon EV", 0.30, 0.80),
            PlugInEvent(3, 2, "Tata Tiago EV", 0.25, 0.80),
            PlugInEvent(6, 3, "MG ZS EV", 0.35, 0.80),
            PlugInEvent(9, 4, "Mahindra XUV400", 0.20, 0.80),
            PlugInEvent(12, 5, "BYD Atto 3", 0.40, 0.80),
            PlugInEvent(15, 6, "Hyundai Kona", 0.30, 0.80),
        ),
        fault=FaultEvent(25, 3),  # charger 3 is CP003
    ),
}

# --------------------------------------------------------------------------------------------
# Tunables (real seconds unless stated)
# --------------------------------------------------------------------------------------------

RESET_STOP_TIMEOUT_S = 3.0  # contract 6b: RemoteStop + waiting for the StopTransactions, in total
RESET_POLL_INTERVAL_S = 0.1  # how often the reset looks at the sessions it asked to stop
RESET_TICK_REASON = "demo_reset"
# The scenario whose start every reset puts the simulation clock back to (see reset_start()).
RESET_CLOCK_SCENARIO = "evening_rush"

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

# Fault injection (Scenario.fault).
FAULT_MESSAGE_ID = "SimFault"  # DataTransfer messageId the simulated charge point faults on
FAULT_CALL_TIMEOUT_S = 5.0  # how long the charge point has to answer that DataTransfer
FAULT_CONFIRM_TIMEOUT_S = 5.0  # and to report Faulted afterwards
FAULT_POLL_INTERVAL_S = 0.1  # how often the charger's status is read while waiting for it
FAULTED = ChargePointStatus.faulted.value

SESSION_ACTIVE = "active"  # Session.status of a session in progress
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

# Truncated in this order (children first); CASCADE covers anything else referencing sessions.
_RESET_TABLES = (MeterValue.__tablename__, Schedule.__tablename__, Session.__tablename__)

# Status strings of the per-charge-point reset report besides the charge point's answer.
STOP_NOT_CONNECTED = "NotConnected"  # never (re)connected while the reset waited
STOP_NO_TRANSACTION = "NoTransaction"  # a session in the database with no transaction id
STOP_UNKNOWN_TRANSACTION = "UnknownTransaction"  # transacting, but its id never became known

# Event statuses in GET /api/demo/status.
EVENT_PENDING = "pending"
EVENT_PLUGGED_IN = "plugged_in"
EVENT_FAILED = "failed"
EVENT_SKIPPED = "skipped"
EVENT_FAULTED = "faulted"  # the fault only: the charger reported Faulted


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


def reset_start() -> datetime:
    """The instant a demo reset puts the simulation clock back to: the start of the headline
    scenario (``RESET_CLOCK_SCENARIO``), i.e. 18:30 site time on today's real date.

    A reset has to leave the clock somewhere defined, or the demo is not repeatable. Simulated
    time runs at TIME_SCALE (60), so it drifts a simulated hour every real minute, and everything
    read off it drifts with it: the carbon and price curves of the horizon (both shaped by the
    time of day), how many hours a driver who says "I leave at 7am" actually has, and therefore
    the plan, the ETA and the savings. A reset that left the clock where the last run had carried
    it made the same unscripted flow report "at risk, 55%, -476 gCO2" at 05:40 and "on time,
    4.19 kg" at 10:20 -- the same script telling two opposite stories.

    Why this instant and not, say, the real time of day: 18:30 is where the demo's story lives
    (the evening peak, with the overnight carbon trough ahead of it, so a car plugged in by hand
    straight after a reset has something to optimise into), it is the state a presenter expects
    after pressing Reset -- the next button pressed is almost always "Evening rush", which sets
    this very instant -- and it is the only choice that makes a bare reset and a scenario start
    agree. The real time of day would be a different instant on every run, which is the bug.
    TODAY's real date, for the reason ``scenario_start`` gives: the simulated date drifts, the
    real one does not, so repeated runs stay identical.
    """
    return scenario_start(get_scenario(RESET_CLOCK_SCENARIO))


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


@dataclass
class _StopTarget:
    """One charge point the reset has to bring back to Available.

    ``session_id`` is the active session the database has for it, or None for a transaction only
    the charge point itself (and ``registry.live_transactions``) knows about -- what a backend
    restart leaves behind. ``transaction_id`` is None while no id is known: nothing is sent then,
    because a RemoteStopTransaction is never sent with a guessed id. ``answer`` is what the charge
    point said, or why nothing was sent, and is reported as ``remote_stop``; the wait loop below
    fills it in for every target, so its initial value never reaches the response.
    """

    session_id: int | None
    transaction_id: int | None
    charger_id: int
    ocpp_id: str
    answer: str = STOP_NOT_CONNECTED
    sent: bool = False  # one RemoteStopTransaction per target and per reset, never a second


_reset_lock = asyncio.Lock()  # one reset at a time (endpoint and scenarios)


def _db_active_sessions() -> list[_StopTarget]:
    """The sessions the database still has in progress, as targets to stop."""
    with SessionLocal() as db:
        rows = db.execute(
            select(Session.id, Session.ocpp_transaction_id, Charger.id, Charger.ocpp_id)
            .join(Session.charger)
            .where(Session.status == SESSION_ACTIVE)
            .order_by(Session.id)
        ).all()
    return [_StopTarget(*row) for row in rows]


def _db_chargers() -> list[tuple[int, str, str]]:
    """(id, ocpp_id, status) of every charger, in id order.

    ``status`` is what the CSMS last stored from a StatusNotification, so it is how the reset
    sees a charge point that is still transacting without a session in the database: after a
    restart the charge points reconnect and report Charging again.
    """
    with SessionLocal() as db:
        rows = db.execute(
            select(Charger.id, Charger.ocpp_id, Charger.status).order_by(Charger.id)
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


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


async def _collect_targets() -> list[_StopTarget]:
    """Every charge point the reset has to bring back to Available (module docstring, step 2).

    The database's active sessions, plus the charge points that are transacting without one: a
    connector the CSMS last saw in any state but Available, or a live transaction id in
    ``registry.live_transactions``. Connectedness is deliberately not a condition here -- the
    reset that wedged the demo asked six charge points that reconnected nine milliseconds later,
    and the wait below is what gives them those milliseconds. The cost is that a reset with a
    charge point that never comes back takes the whole ``RESET_STOP_TIMEOUT_S``; it then says so
    on that charge point, which is the truth about the demo at that moment. Everything seeds as
    "Available", so a site whose charge points have never connected costs nothing.
    """
    targets = await asyncio.to_thread(_db_active_sessions)
    have_session = {target.ocpp_id for target in targets}
    for charger_id, ocpp_id, status in await asyncio.to_thread(_db_chargers):
        if ocpp_id in have_session:
            continue
        transaction_id = registry.transaction_of(ocpp_id)
        if transaction_id is None and status == ChargePointStatus.available.value:
            continue
        targets.append(_StopTarget(None, transaction_id, charger_id, ocpp_id))
    return targets


async def _send_stop(target: _StopTarget, deadline: float) -> None:
    """Send this target its RemoteStopTransaction if it can take one now; record what happened.

    At most one is ever sent: a charge point that answered is not asked twice, and an answer that
    re-sending would not change is not repeated either. The retries that matter are the two where
    nothing could be sent at all -- a charge point that has not (re)connected yet, and a
    transaction whose id the CSMS has not learned yet -- and both are re-read here on every poll
    of the wait loop, so a charge point that appears mid-reset is still stopped by it.
    """
    if target.sent:
        return
    conn = registry.get(target.ocpp_id)
    if conn is None:
        target.answer = STOP_NOT_CONNECTED
        return
    if target.transaction_id is None:
        # A transaction only the charge point knows: its id reaches the CSMS with the next
        # MeterValues, which may well be within this reset's budget.
        target.transaction_id = registry.transaction_of(target.ocpp_id)
    if target.transaction_id is None:
        target.answer = (
            STOP_NO_TRANSACTION if target.session_id is not None else STOP_UNKNOWN_TRANSACTION
        )
        return
    target.sent = True
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        target.answer = await asyncio.wait_for(
            conn.cp.remote_stop(target.transaction_id), remaining
        )
    except TimeoutError:
        target.answer = STATUS_TIMEOUT


async def _stop_and_wait(
    targets: list[_StopTarget], deadline: float
) -> tuple[set[int], set[int]]:
    """Stop every target, all at once, and wait until their sessions are no longer active and
    their chargers report Available again (a charge point takes a new car only then), or until
    ``deadline``. Every poll first gives the targets nothing could be sent to another go.
    Returns what is still outstanding: (active session ids, charger ids not Available)."""
    if not targets:
        return set(), set()
    loop = asyncio.get_running_loop()
    session_ids = [t.session_id for t in targets if t.session_id is not None]
    charger_ids = sorted({t.charger_id for t in targets})
    while True:
        await asyncio.gather(*(_send_stop(target, deadline) for target in targets))
        still_active, not_available = await asyncio.to_thread(
            _db_stop_progress, session_ids, charger_ids
        )
        remaining = deadline - loop.time()
        if (not still_active and not not_available) or remaining <= 0:
            return still_active, not_available
        await asyncio.sleep(min(RESET_POLL_INTERVAL_S, remaining))


def _target_stopped(
    target: _StopTarget, still_active: set[int], not_available: set[int]
) -> bool:
    """True when this charge point really is done: Available again, with no session of its own
    still running. What it answered does not decide it -- an Accepted RemoteStopTransaction whose
    StopTransaction never arrived has stopped nothing."""
    return target.charger_id not in not_available and target.session_id not in still_active


async def perform_reset() -> dict[str, Any]:
    """Reset all demo state (see the module docstring). Seed data and grid_data survive, and the
    simulation clock goes back to ``reset_start()`` (18:30 site time on today's real date).

    Returns ``{"reset", "sessions": [{"session_id", "ocpp_id", "remote_stop", "stopped"}],
    "waited_s", "truncated"}``: one entry per charge point that had to be brought back to
    Available -- the session the database had for it (null for a transaction only the charge
    point knew about), what it answered to RemoteStopTransaction ("NotConnected" when it never
    turned up, "UnknownTransaction" when its transaction id never became known, so nothing could
    be sent), and ``stopped``: whether it really is Available again with no session left running.
    ``reset`` is true only when every one of them is. Database errors propagate.
    """
    async with _reset_lock:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + RESET_STOP_TIMEOUT_S

        _abort_pending_plug_ins()
        targets = await _collect_targets()
        still_active, not_available = await _stop_and_wait(targets, deadline)
        waited_s = loop.time() - started
        if still_active or not_available:
            logger.warning(
                "Demo reset: after %.1f s sessions %s are still active and chargers %s not "
                "Available; truncating anyway",
                waited_s, sorted(still_active), sorted(not_available),
            )
        # What the sessions table is about to stop remembering. A charge point that did not come
        # back keeps its transaction id here, so the next reset has something to stop it with;
        # one that did has nothing left to run.
        for target in targets:
            if target.charger_id not in not_available:
                registry.forget_transaction(target.ocpp_id)
            elif target.transaction_id is not None:
                registry.note_transaction(target.ocpp_id, target.transaction_id)

        await _truncate()
        truncated_at = loop.time()
        registry.manual_limits_w.clear()
        _abort_pending_plug_ins()
        # The clock goes back only here, and only inside the reset lock: the tables are empty and
        # no plug-in is in flight, so nothing is left holding a deadline derived from the time
        # being left behind. Forward is the dangerous direction (a reset pressed before 18:30
        # site time moves the clock forward, e.g. from a workplace_solar run), and it cannot
        # strand a session either, because there is no session left to strand. Nothing caches
        # "now": the grid data is keyed by timestamp, so the tick below reads the slots of the
        # new horizon instead of the ones cached for the old one, and the tick scheduler's own
        # interval is REAL seconds, so it keeps ticking across the jump. The charge points run
        # clocks of their own and re-anchor to ours on their next Heartbeat.
        clock.set_now(reset_start())
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
        tick_s = loop.time() - truncated_at

    stopped = [_target_stopped(t, still_active, not_available) for t in targets]
    # The three phases are logged separately because only the first is this module's budget: a
    # reset that takes seconds is nearly always waiting for TRUNCATE's exclusive lock or for the
    # tick in flight, not for the charge points.
    logger.info(
        "Demo reset: %d charge point(s) to stop, %d back to Available in %.1f s; "
        "tables %s truncated (stop %.2f s, truncate %.2f s, clock+tick %.2f s)",
        len(targets), sum(stopped), waited_s, ", ".join(_RESET_TABLES),
        waited_s, truncated_at - started - waited_s, tick_s,
    )
    return {
        "reset": all(stopped),
        "sessions": [
            {
                "session_id": target.session_id,
                "ocpp_id": target.ocpp_id,
                "remote_stop": str(target.answer),
                "stopped": is_stopped,
            }
            for target, is_stopped in zip(targets, stopped)
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
    fault: dict[str, Any] | None = None  # the fault's record, for a scenario that has one
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
            "fault": dict(self.fault) if self.fault is not None else None,
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
    """GET /api/demo/status: ``{"scenario", "running", "step", "total_steps", "events", "fault",
    "error"}``. ``step`` counts the plug-ins done; each event records its schedule and, once
    done, the session it created. ``fault`` is null unless the scenario injects one."""
    if _run is None:
        return {
            "scenario": None,
            "running": False,
            "step": 0,
            "total_steps": 0,
            "events": [],
            "fault": None,
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
    fault = None
    if scenario.fault is not None:
        due = start + timedelta(minutes=scenario.fault.offset_min)
        fault = {
            "offset_min": scenario.fault.offset_min,
            "scheduled_at": due.isoformat(),
            "local_time": _local_hhmm(due),
            "charger_id": scenario.fault.charger_id,
            "status": EVENT_PENDING,
            "requested_at": None,
            "ocpp_id": None,
            "charger_status": None,
            "detail": None,
        }
    return _ScenarioRun(scenario=scenario, events=events, fault=fault)


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


def _db_charger(charger_id: int) -> tuple[str, str] | None:
    """(ocpp_id, status) of the charger, or None when there is no such charger."""
    with SessionLocal() as db:
        row = db.execute(
            select(Charger.ocpp_id, Charger.status).where(Charger.id == charger_id)
        ).first()
    return (row[0], row[1]) if row is not None else None


async def _wait_for_faulted(charger_id: int) -> str | None:
    """Wait until the charger reports Faulted, for at most FAULT_CONFIRM_TIMEOUT_S; returns the
    status last read (the charge point sends its StatusNotification after answering the fault)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FAULT_CONFIRM_TIMEOUT_S
    while True:
        charger = await asyncio.to_thread(_db_charger, charger_id)
        status = charger[1] if charger is not None else None
        remaining = deadline - loop.time()
        if status == FAULTED or remaining <= 0:
            return status
        await asyncio.sleep(min(FAULT_POLL_INTERVAL_S, remaining))


async def _inject_fault(fault: FaultEvent, record: dict[str, Any]) -> None:
    """Send the fault to the charger's charge point and wait for the Faulted status it reports.

    The charge point answers the DataTransfer, drops its power to 0 kW and sends
    StatusNotification(Faulted); the CSMS handler stores that status and asks the orchestrator for
    the re-plan, so none is requested here. ``record`` gets what the charge point answered.
    Raises RuntimeError when the fault could not be delivered or was not reported back.
    """
    record["requested_at"] = clock.now().isoformat()
    charger = await asyncio.to_thread(_db_charger, fault.charger_id)
    if charger is None:
        raise RuntimeError(f"Charger {fault.charger_id} not found")
    ocpp_id = charger[0]
    record["ocpp_id"] = ocpp_id
    conn = registry.get(ocpp_id)
    if conn is None:
        raise RuntimeError(f"Charger {fault.charger_id} ({ocpp_id}) is not connected to the CSMS")
    what = f"DataTransfer({PLUG_IN_VENDOR_ID}/{FAULT_MESSAGE_ID})"
    payload = call.DataTransfer(vendor_id=PLUG_IN_VENDOR_ID, message_id=FAULT_MESSAGE_ID)
    try:
        result = await asyncio.wait_for(conn.cp.call(payload), FAULT_CALL_TIMEOUT_S)
    except TimeoutError:  # asyncio.TimeoutError is TimeoutError on Python 3.11
        raise RuntimeError(
            f"{ocpp_id} did not answer the fault {what} within {FAULT_CALL_TIMEOUT_S:g} s"
        ) from None
    # suppress=True (the library's default) turns a CALLERROR answer into None.
    status = STATUS_CALL_ERROR if result is None else str(result.status)
    record["detail"] = f"{what} -> {status}"
    if status != DataTransferStatus.accepted:
        raise RuntimeError(f"{ocpp_id} answered the fault {what} with status {status!r}")
    record["charger_status"] = await _wait_for_faulted(fault.charger_id)
    if record["charger_status"] != FAULTED:
        raise RuntimeError(
            f"{ocpp_id} accepted the fault but is {record['charger_status']!r}, not {FAULTED!r}, "
            f"{FAULT_CONFIRM_TIMEOUT_S:g} s later"
        )


async def _fault_step(run: _ScenarioRun, t0: float) -> None:
    """Wait until the fault is due -- ``offset_min`` simulated minutes after ``t0``, the moment the
    plug-ins are timed from too -- then inject it. A failure sets ``run.error``: this scenario IS
    the fault, so it must not report success without it."""
    fault, record = run.scenario.fault, run.fault
    if fault is None or record is None:
        return
    loop = asyncio.get_running_loop()
    due = t0 + clock.sim_seconds_to_real(fault.offset_min * SECONDS_PER_MINUTE)
    delay = due - loop.time()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await _inject_fault(fault, record)
    except Exception as exc:
        record["status"] = EVENT_FAILED
        run.error = f"Fault injection on charger {fault.charger_id} failed: {describe_error(exc)}"
        logger.error("Scenario %s: %s", run.scenario.name, run.error)
        return
    record["status"] = EVENT_FAULTED
    logger.info(
        "Scenario %s: fault injected on charger %d (%s), which now reports %s",
        run.scenario.name, fault.charger_id, record["ocpp_id"], record["charger_status"],
    )


def describe_error(exc: BaseException) -> str:
    """A one-line description of an unexpected failure for ``error``. SQLAlchemy errors are
    reduced to the database driver's message."""
    if isinstance(exc, SQLAlchemyError) and getattr(exc, "orig", None) is not None:
        message = " ".join(str(exc.orig).split())
        return f"Database error ({type(exc.orig).__name__}): {message}"
    return f"{type(exc).__name__}: {exc}"


def _skip_pending(run: _ScenarioRun) -> None:
    records = run.events if run.fault is None else [*run.events, run.fault]
    for record in records:
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

    await _fault_step(run, t0)


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
        injected = "" if run.fault is None else f", fault on charger {run.fault['charger_id']}"
        logger.info("Scenario %s: all %d plug-ins done%s", scenario.name, run.step, injected)
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
