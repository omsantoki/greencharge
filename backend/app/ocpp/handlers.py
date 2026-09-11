"""The Central System side of one OCPP 1.6J charge-point connection.

``app.ocpp.csms`` creates one ``CentralSystemChargePoint`` (an ``ocpp.v16.ChargePoint``) per
connected charge point. It answers the six CP -> CSMS messages of the build spec and offers the
CSMS -> CP calls used by the API (and, from Phase 4, the orchestrator).

Inbound (CP -> CSMS), one ``@on`` handler each:

    BootNotification    Accepted, interval = settings.heartbeat_interval_s, current time.
    Heartbeat           chargers.last_heartbeat = now; replies with the current time.
    StatusNotification  chargers.status = the reported status.
    StartTransaction    Creates the Session from the plug-in parameters the API left in
                        ``registry.pending_plugins``; transaction id = Session.id (also stored in
                        ``ocpp_transaction_id``). With no pending parameters the reply is
                        idTagInfo "Invalid" with transaction id 0, and the CP must not charge.
                        ``registry.session_waiters`` is resolved with the session id once the
                        reply has been SENT (``@after`` hook), so the charge point knows its
                        transaction id before anyone can act on the new session.
    MeterValues         Writes one MeterValue row and updates the session's energy_delivered_kwh
                        and soc_current (see "MeterValues" below).
    StopTransaction     Session status "completed", energy_delivered_kwh = meterStop / 1000, and
                        the session's entry in ``registry.manual_limits_w`` is dropped.

Outbound (CSMS -> CP): ``set_charging_profile``, ``remote_stop``, ``send_plug_in``. Each returns
the status string the charge point answered ("Accepted", "Rejected", ...), or "CallError" (the
charge point answered with a CALLERROR), "Timeout" (no answer within the response timeout) or
"Error" (the call could not be made: connection closed, invalid payload, ...). They never raise;
only task cancellation propagates.

Rules this module follows:

- DEADLOCK RULE: an ``@on`` handler runs inline on the connection's receive loop, so it never
  awaits an outbound ``self.call(...)``: the reply to that call could only be read after the
  handler returned, so the call would hang until its timeout. Follow-up work runs in an
  ``@after`` hook (after the reply is sent) or in a separately scheduled task. The outbound
  helpers below must not be awaited from inside an ``@on`` handler for the same reason.
- Time: every timestamp written to the database is ``clock.now()`` (the simulation clock), read
  when the message is received. Timestamps sent by the charge point are logged, never stored.
- Database: each handler runs one short transaction in its own ``SessionLocal()``, in a worker
  thread (``asyncio.to_thread``) so a slow or unreachable database never stalls the event loop
  that also serves the API and the other charge points. The in-memory registry and asyncio
  futures are only touched on the event-loop thread.
- A database failure is logged and the charge point still gets a valid reply. For
  StartTransaction that reply is "Invalid" (no session exists, so the CP must not charge).

MeterValues: sampled values are read by measurand -- Power.Active.Import (W or kW -> kW),
Energy.Active.Import.Register (Wh or kWh -> kWh) and SoC (Percent -> 0.0-1.0). OCPP's defaults
apply to omitted fields (measurand Energy.Active.Import.Register, energy unit Wh); a power value
without a unit is read as W and a SoC value without a unit as Percent. Other measurands,
per-phase samples and unsupported units are ignored. When one message carries several meterValue
entries, later entries override earlier ones, since all of them get the same receipt timestamp.
The row needs all three quantities, so a message lacking one of them only updates the session
fields it does carry (nothing is filled in or guessed). The session is the charger's active
session with the message's transactionId, or, when the message has no transactionId, the
charger's active session. Readings for unknown or finished sessions are dropped.
"""
import asyncio
import itertools
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ocpp.routing import after, on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.datatypes import IdTagInfo
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingRateUnitType,
    Measurand,
    RegistrationStatus,
    UnitOfMeasure,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.clock import clock
from app.config import settings
from app.db import SessionLocal
from app.models import Charger, MeterValue, Session
from app.ocpp import registry

logger = logging.getLogger("greencharge.ocpp.handlers")

# Session.status values (models.Session: active|completed|aborted).
SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"

# From the spec's SetChargingProfile payload: the chargers have one connector, profiles use
# stack level 0, and the schedule starts with one period at offset 0.
CONNECTOR_ID = 1
STACK_LEVEL = 0
FIRST_PERIOD_START_S = 0

# The plug-in relay: the DataTransfer vendorId / messageId the simulated charge point acts on.
PLUG_IN_VENDOR_ID = "GreenCharge"
PLUG_IN_MESSAGE_ID = "SimPlugIn"

# What the outbound helpers return when the charge point gave no status.
STATUS_CALL_ERROR = "CallError"  # the charge point answered with a CALLERROR
STATUS_TIMEOUT = "Timeout"  # no answer within the connection's response timeout
STATUS_ERROR = "Error"  # the call could not be made (connection closed, invalid payload, ...)

WH_PER_KWH = 1000.0
W_PER_KW = 1000.0
PERCENT_PER_UNIT = 100.0

# measurand -> (MeterReading field, {unit: divisor to kW / kWh / 0..1}, unit when none is sent)
_MEASURANDS: dict[str, tuple[str, dict[str, float], str]] = {
    Measurand.power_active_import.value: (
        "power_kw",
        {UnitOfMeasure.w.value: W_PER_KW, UnitOfMeasure.kw.value: 1.0},
        UnitOfMeasure.w.value,
    ),
    Measurand.energy_active_import_register.value: (
        "energy_kwh",
        {UnitOfMeasure.wh.value: WH_PER_KWH, UnitOfMeasure.kwh.value: 1.0},
        UnitOfMeasure.wh.value,  # OCPP 1.6 default unit
    ),
    Measurand.soc.value: (
        "soc",
        {UnitOfMeasure.percent.value: PERCENT_PER_UNIT},
        UnitOfMeasure.percent.value,
    ),
}
# OCPP 1.6: a sampled value without "measurand" is an Energy.Active.Import.Register reading.
_DEFAULT_MEASURAND = Measurand.energy_active_import_register.value


# --------------------------------------------------------------------------------------------
# MeterValues parsing (pure)
# --------------------------------------------------------------------------------------------


@dataclass
class MeterReading:
    """The quantities one MeterValues message reported, in kW, kWh and 0.0-1.0 (None = absent)."""

    power_kw: float | None = None
    energy_kwh: float | None = None
    soc: float | None = None

    def is_complete(self) -> bool:
        return None not in (self.power_kw, self.energy_kwh, self.soc)


def parse_meter_values(ocpp_id: str, meter_value: list[dict] | None) -> MeterReading:
    """Read power, energy and SoC from a MeterValues ``meter_value`` list (snake_case keys, as the
    ocpp library passes it: ``[{"timestamp": ..., "sampled_value": [{"value": "5000",
    "measurand": "Power.Active.Import", "unit": "W", ...}, ...]}, ...]``)."""
    reading = MeterReading()
    for entry in meter_value or ():
        for sample in entry.get("sampled_value") or ():
            measurand = sample.get("measurand", _DEFAULT_MEASURAND)
            target = _MEASURANDS.get(measurand)
            if target is None or sample.get("phase"):
                continue  # a quantity we do not store, or one phase only (not the total)
            field, divisors, default_unit = target
            unit = sample.get("unit", default_unit)
            divisor = divisors.get(unit)
            if divisor is None:
                logger.warning("%s: ignoring %s in unsupported unit %r", ocpp_id, measurand, unit)
                continue
            raw = sample.get("value")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                logger.warning("%s: ignoring non-numeric %s value %r", ocpp_id, measurand, raw)
                continue
            setattr(reading, field, value / divisor)
    return reading


# --------------------------------------------------------------------------------------------
# Database work (sync; run in a worker thread, one short transaction each)
# --------------------------------------------------------------------------------------------


def _db_update_charger(ocpp_id: str, **values: Any) -> bool:
    """Set columns on the charger row; False when there is no charger with this ocpp_id."""
    with SessionLocal() as db:
        result = db.execute(update(Charger).where(Charger.ocpp_id == ocpp_id).values(**values))
        found = result.rowcount > 0
        db.commit()
    return found


def _db_create_session(ocpp_id: str, params: dict, now: datetime) -> int:
    """Create the active Session for a StartTransaction; return its id (= transaction id).

    ``params`` are the plug-in parameters from POST /api/debug/plug-in. The deadline is
    ``now + hours_until_departure`` in simulated hours.
    """
    with SessionLocal() as db:
        charger_id = db.scalar(select(Charger.id).where(Charger.ocpp_id == ocpp_id))
        if charger_id is None:
            raise LookupError(f"no charger with ocpp_id {ocpp_id!r}")
        soc_start = float(params["soc_start"])
        session = Session(
            charger_id=charger_id,
            vehicle_model=str(params["vehicle_model"]),
            battery_kwh=float(params["battery_kwh"]),
            max_charge_kw=float(params["max_kw"]),
            soc_start=soc_start,
            soc_target=float(params["soc_target"]),
            soc_current=soc_start,
            plugged_in_at=now,
            deadline=now + timedelta(hours=float(params["hours_until_departure"])),
            status=SESSION_ACTIVE,
        )
        db.add(session)
        db.flush()  # assigns session.id
        session.ocpp_transaction_id = session.id
        db.commit()
        return session.id


def _find_session(
    db: DbSession, ocpp_id: str, transaction_id: int | None, active_only: bool
) -> Session | None:
    """The newest session on this charger with ``transaction_id`` (any, when None)."""
    stmt = select(Session).join(Session.charger).where(Charger.ocpp_id == ocpp_id)
    if transaction_id is not None:
        stmt = stmt.where(Session.ocpp_transaction_id == transaction_id)
    if active_only:
        stmt = stmt.where(Session.status == SESSION_ACTIVE)
    return db.scalars(stmt.order_by(Session.id.desc()).limit(1)).first()


def _db_record_meter_values(
    ocpp_id: str, transaction_id: int | None, reading: MeterReading, now: datetime
) -> None:
    """Store one MeterValues message: the session fields it carries and, when it carries power,
    energy and SoC, one MeterValue row stamped ``now``."""
    with SessionLocal() as db:
        session = _find_session(db, ocpp_id, transaction_id, active_only=True)
        if session is None:
            if transaction_id is not None:
                logger.warning(
                    "%s: MeterValues for transaction %s, which is not an active session on this "
                    "charger; dropped", ocpp_id, transaction_id,
                )
            else:
                logger.debug("%s: MeterValues without a transaction and no active session; "
                             "dropped", ocpp_id)
            return
        if reading.energy_kwh is not None:
            session.energy_delivered_kwh = reading.energy_kwh
        if reading.soc is not None:
            session.soc_current = reading.soc
        if reading.is_complete():
            db.add(
                MeterValue(
                    session_id=session.id,
                    ts=now,
                    power_kw=reading.power_kw,
                    energy_kwh=reading.energy_kwh,
                    soc=reading.soc,
                )
            )
        else:
            logger.info(
                "%s: MeterValues for session %d lack power, energy or SoC (%s); session updated, "
                "no meter-value row written", ocpp_id, session.id, reading,
            )
        db.commit()


def _db_stop_session(ocpp_id: str, transaction_id: int, meter_stop_wh: int) -> int | None:
    """Complete the session of a StopTransaction; return its id, or None if there is none.

    energy_delivered_kwh = meterStop / 1000 (the simulated meter restarts at 0 for every
    transaction). Only an active session becomes "completed"; another status is kept.
    """
    with SessionLocal() as db:
        session = _find_session(db, ocpp_id, transaction_id, active_only=False)
        if session is None:
            return None
        session.energy_delivered_kwh = meter_stop_wh / WH_PER_KWH
        if session.status == SESSION_ACTIVE:
            session.status = SESSION_COMPLETED
        else:
            logger.warning(
                "%s: StopTransaction for session %d, whose status is %r; status kept",
                ocpp_id, session.id, session.status,
            )
        db.commit()
        return session.id


def _start_rejected() -> call_result.StartTransaction:
    """StartTransaction reply that creates no transaction: the CP must not charge."""
    return call_result.StartTransaction(
        transaction_id=0, id_tag_info=IdTagInfo(status=AuthorizationStatus.invalid)
    )


# --------------------------------------------------------------------------------------------
# The charge point connection
# --------------------------------------------------------------------------------------------


class CentralSystemChargePoint(ChargePoint):
    """CSMS side of one OCPP 1.6J connection; ``self.id`` is the charge point's ocpp_id.

    Constructed like ``ocpp.v16.ChargePoint``: ``CentralSystemChargePoint(ocpp_id, connection)``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # chargingProfileId of the next SetChargingProfile on this connection.
        self._profile_ids = itertools.count(1)
        # Handed from on_start_transaction to after_start_transaction (same message; messages
        # of one connection are handled one at a time).
        self._started_session_id: int | None = None

    # ---- inbound: CP -> CSMS ---------------------------------------------------------------

    @on(Action.boot_notification)
    async def on_boot_notification(
        self, charge_point_vendor: str, charge_point_model: str, **kwargs: Any
    ) -> call_result.BootNotification:
        logger.info("%s: BootNotification (%s %s)", self.id, charge_point_vendor, charge_point_model)
        return call_result.BootNotification(
            current_time=clock.now().isoformat(),
            interval=settings.heartbeat_interval_s,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs: Any) -> call_result.Heartbeat:
        now = clock.now()
        await self._update_charger(last_heartbeat=now)
        return call_result.Heartbeat(current_time=now.isoformat())

    @on(Action.status_notification)
    async def on_status_notification(
        self, connector_id: int, error_code: str, status: str, **kwargs: Any
    ) -> call_result.StatusNotification:
        logger.info(
            "%s: StatusNotification connector %s -> %s (error code %s, CP timestamp %s)",
            self.id, connector_id, status, error_code, kwargs.get("timestamp"),
        )
        await self._update_charger(status=status)
        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_transaction(
        self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs: Any
    ) -> call_result.StartTransaction:
        now = clock.now()
        self._started_session_id = None
        params = registry.pending_plugins.pop(self.id, None)
        if params is None:
            logger.warning(
                "%s: StartTransaction (id_tag %r) without a pending plug-in; replied Invalid",
                self.id, id_tag,
            )
            return _start_rejected()
        try:
            session_id = await asyncio.to_thread(_db_create_session, self.id, params, now)
        except Exception:
            logger.exception(
                "%s: could not create the session for StartTransaction; replied Invalid", self.id
            )
            return _start_rejected()
        self._started_session_id = session_id
        logger.info(
            "%s: StartTransaction connector %s, meter_start %s Wh, CP timestamp %s -> session "
            "and transaction %d (%s)",
            self.id, connector_id, meter_start, timestamp, session_id, params.get("vehicle_model"),
        )
        return call_result.StartTransaction(
            transaction_id=session_id, id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted)
        )

    @after(Action.start_transaction)
    def after_start_transaction(self, **kwargs: Any) -> None:
        """Runs once the StartTransaction reply has been sent: resolve the plug-in waiter."""
        session_id, self._started_session_id = self._started_session_id, None
        if session_id is None:
            return
        # An exception here would end the connection's receive loop, so none may escape.
        try:
            waiter = registry.session_waiters.get(self.id)
            if waiter is not None and not waiter.done():
                waiter.set_result(session_id)
        except Exception:
            logger.exception("%s: could not resolve the plug-in waiter", self.id)

    @on(Action.meter_values)
    async def on_meter_values(
        self,
        connector_id: int,
        meter_value: list[dict],
        transaction_id: int | None = None,
        **kwargs: Any,
    ) -> call_result.MeterValues:
        now = clock.now()
        reading = parse_meter_values(self.id, meter_value)
        try:
            await asyncio.to_thread(_db_record_meter_values, self.id, transaction_id, reading, now)
        except Exception:
            logger.exception("%s: could not store MeterValues", self.id)
        return call_result.MeterValues()

    @on(Action.stop_transaction)
    async def on_stop_transaction(
        self, meter_stop: int, timestamp: str, transaction_id: int, **kwargs: Any
    ) -> call_result.StopTransaction:
        try:
            session_id = await asyncio.to_thread(
                _db_stop_session, self.id, transaction_id, meter_stop
            )
        except Exception:
            logger.exception("%s: could not complete transaction %s", self.id, transaction_id)
            session_id = None
        if session_id is None:
            logger.warning(
                "%s: StopTransaction for transaction %s, no session updated", self.id, transaction_id
            )
        else:
            registry.manual_limits_w.pop(session_id, None)
            logger.info(
                "%s: StopTransaction transaction %s (reason %s, meter_stop %s Wh, CP timestamp "
                "%s) -> session %d completed",
                self.id, transaction_id, kwargs.get("reason"), meter_stop, timestamp, session_id,
            )
        return call_result.StopTransaction()

    async def _update_charger(self, **values: Any) -> None:
        try:
            found = await asyncio.to_thread(_db_update_charger, self.id, **values)
        except Exception:
            logger.exception("%s: could not update charger %s", self.id, ", ".join(values))
            return
        if not found:
            logger.warning("%s: no charger row with this ocpp_id", self.id)

    # ---- outbound: CSMS -> CP (never await these inside an @on handler) --------------------

    async def set_charging_profile(self, transaction_id: int, limit_w: float) -> str:
        """Push a power limit (W) for the transaction: the spec's SetChargingProfile payload, a
        TxProfile covering one slot from now, ``limit`` sent as an integer number of watts."""
        what = f"SetChargingProfile(transaction {transaction_id}, {limit_w} W)"
        try:
            payload = call.SetChargingProfile(
                connector_id=CONNECTOR_ID,
                cs_charging_profiles={
                    "chargingProfileId": next(self._profile_ids),
                    "stackLevel": STACK_LEVEL,
                    "chargingProfilePurpose": ChargingProfilePurposeType.tx_profile,
                    "chargingProfileKind": ChargingProfileKindType.absolute,
                    "transactionId": int(transaction_id),
                    "chargingSchedule": {
                        # One slot: the spec's 900 s = settings.slot_minutes (15) * 60.
                        "duration": settings.slot_minutes * 60,
                        "startSchedule": clock.now().isoformat(),
                        "chargingRateUnit": ChargingRateUnitType.watts,
                        "chargingSchedulePeriod": [
                            {"startPeriod": FIRST_PERIOD_START_S, "limit": int(round(limit_w))}
                        ],
                    },
                },
            )
        except Exception:
            logger.exception("%s: could not build %s", self.id, what)
            return STATUS_ERROR
        return await self._call_for_status(payload, what)

    async def remote_stop(self, transaction_id: int) -> str:
        """Ask the charge point to stop the transaction (operator override)."""
        what = f"RemoteStopTransaction(transaction {transaction_id})"
        try:
            payload = call.RemoteStopTransaction(transaction_id=int(transaction_id))
        except Exception:
            logger.exception("%s: could not build %s", self.id, what)
            return STATUS_ERROR
        return await self._call_for_status(payload, what)

    async def send_plug_in(self, params: dict) -> str:
        """Tell the simulated charge point a car was plugged in (DataTransfer GreenCharge/SimPlugIn).

        ``data`` is sent as a JSON string: the ocpp library rewrites dict keys containing "soc"
        (e.g. "soc_start") on the way out, so the parameters must not travel as a dict.
        """
        what = f"DataTransfer({PLUG_IN_VENDOR_ID}/{PLUG_IN_MESSAGE_ID})"
        try:
            payload = call.DataTransfer(
                vendor_id=PLUG_IN_VENDOR_ID, message_id=PLUG_IN_MESSAGE_ID, data=json.dumps(params)
            )
        except Exception:
            logger.exception("%s: could not build %s", self.id, what)
            return STATUS_ERROR
        return await self._call_for_status(payload, what)

    async def _call_for_status(self, payload: Any, what: str) -> str:
        """Send one call and return the charge point's status string; never raises."""
        try:
            result = await self.call(payload)
        except asyncio.TimeoutError:
            logger.warning("%s: no answer to %s in time", self.id, what)
            return STATUS_TIMEOUT
        except Exception:
            logger.exception("%s: %s failed", self.id, what)
            return STATUS_ERROR
        if result is None:  # suppress=True turns a CALLERROR answer into None
            logger.warning("%s: %s was answered with a CALLERROR", self.id, what)
            return STATUS_CALL_ERROR
        status = str(result.status)
        logger.info("%s: %s -> %s", self.id, what, status)
        return status
