"""One simulated OCPP 1.6J charge point (CP) with a single connector (connector 1).

Behaviour:

* On connect: BootNotification (retried until Accepted), a Heartbeat right away and then every
  ``interval`` seconds from the Boot reply, then a StatusNotification for connector 1.
* Plug-in: the CSMS relays a plug-in event as DataTransfer(vendorId "GreenCharge",
  messageId "SimPlugIn", data = JSON string with vehicle_model, battery_kwh, max_kw, soc_start,
  soc_target, hours_until_departure). The handler only validates the request and replies
  Accepted or Rejected. The sequence StatusNotification(Preparing) -> StartTransaction ->
  StatusNotification(Charging) runs in a separate task that starts after the reply has been sent.
  DEADLOCK RULE: an @on handler runs inline on the receive loop, so it never awaits an outbound
  call; follow-up calls always run in their own task.
* Fault injection (the demo's "fault_injection" scenario): the CSMS relays a fault as
  DataTransfer(vendorId "GreenCharge", messageId "SimFault"). The handler replies Accepted, and a
  task started after the reply drops the connector's limit to 0 kW and reports
  StatusNotification(Faulted). The transaction stays open, drawing nothing, until the car leaves.
* Charging: the battery advances every 1 real second with the spec's ``step()``; the power drawn is
  ``min(limit_kw, acceptance_kw(soc, max_kw))``. The limit is the charger rating until a
  SetChargingProfile (TxProfile, unit W) replaces it with its period-0 limit. The energy register
  restarts at 0 Wh with every StartTransaction (meter_start = 0) and counts the energy drawn by the
  charger; the battery gains that energy times the ``step()`` efficiency.
* MeterValues every 10 real seconds: Power.Active.Import (W), Energy.Active.Import.Register (Wh) and
  SoC (Percent), context Sample.Periodic.
* Stop: soc >= soc_target -> reason Local; simulated time since StartTransaction >= the departure
  time -> reason EVDisconnected; RemoteStopTransaction -> reason Remote. The CP then sends a last
  MeterValues (context Transaction.End, so the CSMS records the final SoC and energy),
  StopTransaction, and StatusNotification Finishing -> Available.

Time: TIME_SCALE compresses SIMULATED time only (physics dt, the departure countdown, message
timestamps): ``dt_hours = real_elapsed * TIME_SCALE / 3600``. Message cadence (the Heartbeat
interval, MeterValues every 10 s, physics every 1 s) is in real wall-clock seconds. Message
timestamps come from ``SimClock``, re-synced to the CSMS's currentTime on every Boot and Heartbeat
reply.

Reconnects: ``ChargerState`` holds the connector's transaction and outlives a single WebSocket
connection; run.py keeps one per charger and hands it to every new ``SimChargePoint``. If the CSMS
goes away mid-transaction the car keeps charging: on the next connection the physics catches up
the offline time (still in 1-second steps, applying the stop rules), MeterValues resume under the
same transactionId, and a stop that happened while offline is reported then. MeterValues are not
sent while offline and are not queued.
"""
import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ocpp.routing import after, on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    DataTransferStatus,
    Measurand,
    ReadingContext,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    UnitOfMeasure,
)
from websockets.exceptions import ConnectionClosed

from battery import acceptance_kw, step

log = logging.getLogger("greencharge.simulator")

# Cadence, in REAL seconds (see the module docstring).
METER_INTERVAL_S = 10.0  # spec Phase 2: MeterValues every 10 seconds
PHYSICS_INTERVAL_S = 1.0  # physics step: every 1 real second
RETRY_DELAY_S = 2.0  # reconnect delay while the CSMS is down; also the BootNotification retry fallback

# Protocol identifiers.
CONNECTOR_ID = 1  # one connector per CP (the spec's SetChargingProfile payload targets connector 1)
VENDOR_ID = "GreenCharge"  # DataTransfer vendorId of the plug-in relay
PLUG_IN_MESSAGE_ID = "SimPlugIn"  # DataTransfer messageId of the plug-in relay
FAULT_MESSAGE_ID = "SimFault"  # DataTransfer messageId of the fault-injection relay
FAULT_ERROR_CODE = ChargePointErrorCode.ground_failure  # error code reported with Faulted
CHARGE_POINT_VENDOR = "GreenCharge"  # BootNotification chargePointVendor (max 20 chars)
CHARGE_POINT_MODEL = "SimChargePoint"  # BootNotification chargePointModel (max 20 chars)
ID_TAG_MAX_LEN = 20  # OCPP 1.6 idTag is at most 20 characters

# Unit conversions.
SECONDS_PER_HOUR = 3600.0
W_PER_KW = 1000.0
WH_PER_KWH = 1000.0
PERCENT = 100.0

# Numerics: halvings used to find the moment inside a step when soc_target is reached
# (2**-40 of a step is far below any meaningful time or energy).
BISECTION_ITERATIONS = 40

# Connector phases of the CP-side state machine.
IDLE = "idle"  # Available, ready for a plug-in
PREPARING = "preparing"  # plug-in accepted, StartTransaction not answered yet
CHARGING = "charging"  # transaction running
FINISHING = "finishing"  # stop decided, stop sequence in progress


class SimClock:
    """Simulated UTC clock: sim_now = sim_anchor + (monotonic - mono_anchor) * time_scale.

    Starts at the real UTC time; ``sync()`` re-anchors it to the CSMS's currentTime. A simulator
    process shares one instance between all its charge points (one asyncio thread, so no lock).
    """

    def __init__(self, time_scale: float) -> None:
        if not (math.isfinite(time_scale) and time_scale > 0):
            raise ValueError(f"time_scale must be a positive number, got {time_scale!r}")
        self._time_scale = float(time_scale)
        self._sim_anchor = datetime.now(timezone.utc)
        self._mono_anchor = time.monotonic()

    @property
    def time_scale(self) -> float:
        """Simulated seconds per real second (TIME_SCALE)."""
        return self._time_scale

    def now(self) -> datetime:
        """Current simulated time, timezone-aware UTC."""
        elapsed_real_s = time.monotonic() - self._mono_anchor
        return self._sim_anchor + timedelta(seconds=elapsed_real_s * self._time_scale)

    def set_now(self, sim_dt: datetime) -> None:
        """Make ``sim_dt`` the current simulated time (a naive value is taken as UTC)."""
        if sim_dt.tzinfo is None or sim_dt.tzinfo.utcoffset(sim_dt) is None:
            sim_dt = sim_dt.replace(tzinfo=timezone.utc)
        self._sim_anchor = sim_dt.astimezone(timezone.utc)
        self._mono_anchor = time.monotonic()

    def sync(self, current_time: str) -> None:
        """Re-anchor to an OCPP currentTime string. An unparseable value is logged and ignored."""
        try:
            parsed = datetime.fromisoformat(current_time)
        except (TypeError, ValueError):
            log.warning("ignoring unparseable currentTime %r from the CSMS", current_time)
            return
        self.set_now(parsed)

    def iso_now(self) -> str:
        """Current simulated time as an ISO-8601 string with a UTC offset, for OCPP timestamps."""
        return self.now().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class PlugIn:
    """Vehicle parameters relayed by the CSMS in the SimPlugIn DataTransfer."""

    vehicle_model: str
    battery_kwh: float
    max_kw: float  # the vehicle's acceptance ceiling (the CSMS sends min(vehicle AC max, charger max))
    soc_start: float  # 0.0-1.0
    soc_target: float  # 0.0-1.0
    hours_until_departure: float  # simulated hours


def parse_plug_in(data: Any) -> PlugIn:
    """Validate the SimPlugIn DataTransfer ``data`` (a JSON object encoded as a string).

    Raises ValueError with a short explanation if it is unusable.
    """
    if not isinstance(data, str):
        raise ValueError("data must be a JSON string")
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"data is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("data must be a JSON object")

    def number(key: str) -> float:
        value = obj.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number, got {value!r}")
        return float(value)

    battery_kwh = number("battery_kwh")
    max_kw = number("max_kw")
    soc_start = number("soc_start")
    soc_target = number("soc_target")
    hours_until_departure = number("hours_until_departure")
    vehicle_model = obj.get("vehicle_model", "")
    if not isinstance(vehicle_model, str):
        raise ValueError(f"vehicle_model must be a string, got {vehicle_model!r}")
    if battery_kwh <= 0:
        raise ValueError(f"battery_kwh must be > 0, got {battery_kwh}")
    if max_kw <= 0:
        raise ValueError(f"max_kw must be > 0, got {max_kw}")
    if not 0.0 <= soc_start < soc_target <= 1.0:
        raise ValueError(
            f"need 0 <= soc_start < soc_target <= 1, got soc_start={soc_start}, soc_target={soc_target}"
        )
    if hours_until_departure <= 0:
        raise ValueError(f"hours_until_departure must be > 0, got {hours_until_departure}")
    return PlugIn(vehicle_model, battery_kwh, max_kw, soc_start, soc_target, hours_until_departure)


def parse_tx_profile(connector_id: Any, profile: Any) -> tuple[int | None, float]:
    """Read a SetChargingProfile request: return (transactionId or None, period-0 limit in kW).

    Only a TxProfile for connector 1 with chargingRateUnit "W" is supported; anything else raises
    ValueError. ``profile`` is the snake_case dict the ocpp library passes to the handler.
    """
    if connector_id != CONNECTOR_ID:
        raise ValueError(f"unknown connector {connector_id!r} (this CP has connector {CONNECTOR_ID})")
    if not isinstance(profile, dict):
        raise ValueError("csChargingProfiles must be an object")
    purpose = profile.get("charging_profile_purpose")
    if purpose != ChargingProfilePurposeType.tx_profile:
        raise ValueError(f"charging profile purpose {purpose!r} is not supported, only TxProfile")
    schedule = profile.get("charging_schedule")
    if not isinstance(schedule, dict):
        raise ValueError("chargingSchedule is missing")
    unit = schedule.get("charging_rate_unit")
    if unit != ChargingRateUnitType.watts:
        raise ValueError(f"chargingRateUnit {unit!r} is not supported, only W")
    periods = schedule.get("charging_schedule_period") or []
    period0 = next(
        (p for p in periods if isinstance(p, dict) and p.get("start_period") == 0), None
    )
    if period0 is None:
        raise ValueError("no chargingSchedulePeriod with startPeriod 0")
    try:
        # The library parses SetChargingProfile limits as Decimal (or int); float() takes both.
        limit_w = float(period0.get("limit"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid limit {period0.get('limit')!r}") from exc
    if not math.isfinite(limit_w) or limit_w < 0:
        raise ValueError(f"limit must be a finite number >= 0 W, got {limit_w}")
    transaction_id = profile.get("transaction_id")
    return (None if transaction_id is None else int(transaction_id)), limit_w / W_PER_KW


def _fraction_to_reach(
    soc: float, soc_target: float, battery_kwh: float, power_kw: float, dt_hours: float
) -> float:
    """Smallest fraction f of ``dt_hours`` with step(soc, ..., f * dt_hours) >= soc_target.

    Bisection on the spec's ``step()`` itself (monotonic in time), so the result always satisfies
    the target and no physics constant is duplicated here. Only called when the whole step
    reaches the target.
    """
    lo, hi = 0.0, 1.0
    for _ in range(BISECTION_ITERATIONS):
        mid = (lo + hi) / 2.0
        if step(soc, battery_kwh, power_kw, mid * dt_hours) >= soc_target:
            hi = mid
        else:
            lo = mid
    return hi


def _id_tag_status(id_tag_info: Any) -> str | None:
    """Status of an idTagInfo, which the library hands back as a plain dict."""
    if isinstance(id_tag_info, dict):
        return id_tag_info.get("status")
    return getattr(id_tag_info, "status", None)


@dataclass
class Transaction:
    """A running transaction and the simulated vehicle behind it."""

    transaction_id: int
    vehicle_model: str
    battery_kwh: float
    max_kw: float  # vehicle acceptance ceiling passed to acceptance_kw()
    soc: float
    soc_target: float
    departure_sim_s: float  # simulated seconds after StartTransaction at which the car leaves
    limit_kw: float  # current limit: charger rating until a SetChargingProfile replaces it
    last_mono: float  # time.monotonic() up to which the physics has been integrated
    power_kw: float = 0.0  # power being drawn now
    energy_wh: float = 0.0  # Energy.Active.Import.Register since StartTransaction
    sim_elapsed_s: float = 0.0  # simulated seconds since StartTransaction
    stop_reason: Reason | None = None  # set once a stop rule fires


@dataclass
class ChargerState:
    """Connector state that outlives one WebSocket connection (run.py keeps one per charger)."""

    phase: str = IDLE
    tx: Transaction | None = None
    # A TxProfile that arrived while StartTransaction was in flight: (transactionId or None, kW).
    pending_limit: tuple[int | None, float] | None = None


class SimChargePoint(ChargePoint):
    """One simulated charge point on one WebSocket connection. See the module docstring.

    ``run()`` serves the connection until it closes; build a new instance (with the same
    ``state``) for every new connection.
    """

    def __init__(
        self,
        ocpp_id: str,
        connection: Any,
        clock: SimClock,
        charger_max_kw: float,
        state: ChargerState | None = None,
    ) -> None:
        super().__init__(ocpp_id, connection)
        self.clock = clock
        self.charger_max_kw = float(charger_max_kw)
        self.state = state if state is not None else ChargerState()
        if self.state.tx is None:
            # A plug-in or stop sequence cut short by a lost connection does not resume.
            self.state.phase = IDLE
            self.state.pending_limit = None
        self.id_tag = f"SIM-{ocpp_id}"[:ID_TAG_MAX_LEN]
        self._ready = False  # True once Boot is accepted and the connector status is reported
        self._accepted_plug_in: PlugIn | None = None  # handed from on_ to after_data_transfer
        self._accepted_fault = False  # likewise, for an accepted fault injection
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ connection lifecycle

    async def run(self) -> None:
        """Serve this connection until it ends; re-raise the exception that ended it."""
        receiver = asyncio.create_task(self.start(), name=f"{self.id}:receive")
        main = asyncio.create_task(self._main(), name=f"{self.id}:main")
        try:
            await asyncio.wait({receiver, main}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            children = [receiver, main, *self._tasks]
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        for task in (receiver, main):
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise task.exception()

    async def _main(self) -> None:
        """Boot, first Heartbeat, connector status, then Heartbeats every interval."""
        interval = await self._boot()
        await self._heartbeat()  # the first Heartbeat goes out right after Boot is accepted
        await self._report_connector()
        self._ready = True
        if interval <= 0:
            log.warning("%s: heartbeat interval %s from the CSMS; no periodic heartbeats", self.id, interval)
            await asyncio.get_running_loop().create_future()  # keep the connection open
        while True:
            await asyncio.sleep(interval)
            await self._heartbeat()

    async def _boot(self) -> int:
        """Send BootNotification until Accepted; return the heartbeat interval in seconds."""
        while True:
            result = await self._call(
                call.BootNotification(
                    charge_point_model=CHARGE_POINT_MODEL,
                    charge_point_vendor=CHARGE_POINT_VENDOR,
                )
            )
            retry_s = RETRY_DELAY_S
            if result is not None:
                self.clock.sync(result.current_time)
                if result.status == RegistrationStatus.accepted:
                    log.info(
                        "%s: BootNotification accepted, heartbeat every %s s, CSMS time %s",
                        self.id, result.interval, result.current_time,
                    )
                    return int(result.interval)
                if result.interval > 0:
                    retry_s = result.interval  # OCPP: minimum wait before the next BootNotification
                log.warning("%s: BootNotification %s", self.id, result.status)
            log.warning("%s: retrying BootNotification in %s s", self.id, retry_s)
            await asyncio.sleep(retry_s)

    async def _heartbeat(self) -> None:
        result = await self._call(call.Heartbeat())
        if result is not None:
            self.clock.sync(result.current_time)

    async def _report_connector(self) -> None:
        """Report the connector status; resume a transaction that was open before a reconnect."""
        tx = self.state.tx
        if tx is None:
            await self._status(ChargePointStatus.available)
            return
        log.info("%s: resuming transaction %d on the new connection", self.id, tx.transaction_id)
        if tx.stop_reason is None:
            await self._status(ChargePointStatus.charging)
        self._spawn(self._run_transaction(), "transaction")

    # ------------------------------------------------------------------ outbound helpers

    async def _call(self, payload: Any) -> Any:
        """``self.call()`` that returns None on a CallError or a response timeout (both logged).

        Connection errors propagate: they end this connection and run.py reconnects.
        """
        name = type(payload).__name__
        try:
            result = await self.call(payload)
        except asyncio.TimeoutError:
            log.warning("%s: no response to %s within %s s", self.id, name, self._response_timeout)
            return None
        if result is None:
            log.warning("%s: the CSMS answered %s with a CallError", self.id, name)
        return result

    async def _status(
        self, status: ChargePointStatus, error_code: str = ChargePointErrorCode.no_error
    ) -> None:
        reported = "" if error_code == ChargePointErrorCode.no_error else f" ({error_code})"
        log.info("%s: StatusNotification %s%s", self.id, status, reported)
        await self._call(
            call.StatusNotification(
                connector_id=CONNECTOR_ID,
                error_code=error_code,
                status=status,
                timestamp=self.clock.iso_now(),
            )
        )

    async def _send_meter_values(self, tx: Transaction, context: ReadingContext) -> None:
        def sampled(value: str, measurand: Measurand, unit: UnitOfMeasure) -> dict:
            return {"value": value, "context": context, "measurand": measurand, "unit": unit}

        sampled_values = [
            sampled(f"{tx.power_kw * W_PER_KW:.1f}", Measurand.power_active_import, UnitOfMeasure.w),
            sampled(f"{tx.energy_wh:.1f}", Measurand.energy_active_import_register, UnitOfMeasure.wh),
            sampled(f"{tx.soc * PERCENT:.3f}", Measurand.soc, UnitOfMeasure.percent),
        ]
        log.info(
            "%s: MeterValues tx %d  %.2f kW  %.3f kWh  SoC %.1f%%  (limit %.2f kW, %s)",
            self.id, tx.transaction_id, tx.power_kw, tx.energy_wh / WH_PER_KWH,
            tx.soc * PERCENT, tx.limit_kw, context,
        )
        await self._call(
            call.MeterValues(
                connector_id=CONNECTOR_ID,
                transaction_id=tx.transaction_id,
                meter_value=[{"timestamp": self.clock.iso_now(), "sampled_value": sampled_values}],
            )
        )

    def _spawn(self, coro: Any, name: str) -> None:
        """Run a coroutine as a task owned by this connection (cancelled when it ends)."""
        task = asyncio.create_task(coro, name=f"{self.id}:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None or isinstance(exc, ConnectionClosed):
            return  # a lost connection is handled by run() and the reconnect loop in run.py
        log.error("%s: task %s failed", self.id, task.get_name(), exc_info=exc)

    # ------------------------------------------------------------------ inbound handlers
    # DEADLOCK RULE: these run inline on the receive loop and must never await an outbound call.

    @on(Action.data_transfer)
    def on_data_transfer(
        self, vendor_id: str, message_id: str | None = None, data: str | None = None, **kwargs: Any
    ) -> call_result.DataTransfer:
        """Plug-in and fault-injection relay from the CSMS: validate and reply only.

        The sequence the accepted message starts is spawned by ``after_data_transfer`` once this
        reply has been sent.
        """
        if vendor_id != VENDOR_ID:
            return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)
        if message_id == FAULT_MESSAGE_ID:
            return self._accept_fault()
        if message_id != PLUG_IN_MESSAGE_ID:
            return call_result.DataTransfer(status=DataTransferStatus.unknown_message_id)
        if not self._ready or self.state.phase != IDLE:
            reason = f"connector busy ({self.state.phase})" if self._ready else "boot not complete"
            log.warning("%s: plug-in rejected: %s", self.id, reason)
            return call_result.DataTransfer(status=DataTransferStatus.rejected, data=reason)
        try:
            plug_in = parse_plug_in(data)
        except ValueError as exc:
            log.warning("%s: plug-in rejected: %s", self.id, exc)
            return call_result.DataTransfer(status=DataTransferStatus.rejected, data=str(exc))
        self.state.phase = PREPARING  # reserve the connector now so a second plug-in is rejected
        self._accepted_plug_in = plug_in
        return call_result.DataTransfer(status=DataTransferStatus.accepted)

    def _accept_fault(self) -> call_result.DataTransfer:
        """Accept a fault injection unless this CP has not finished booting."""
        if not self._ready:
            log.warning("%s: fault injection rejected: boot not complete", self.id)
            return call_result.DataTransfer(
                status=DataTransferStatus.rejected, data="boot not complete"
            )
        self._accepted_fault = True
        return call_result.DataTransfer(status=DataTransferStatus.accepted)

    @after(Action.data_transfer)
    def after_data_transfer(self, **kwargs: Any) -> None:
        """Runs after the DataTransfer reply was sent: start the sequence it accepted."""
        plug_in, self._accepted_plug_in = self._accepted_plug_in, None
        if plug_in is not None:
            self._spawn(self._plug_in_sequence(plug_in), "plug-in")
        if self._accepted_fault:
            self._accepted_fault = False
            self._spawn(self._fault_sequence(), "fault")

    @on(Action.set_charging_profile)
    def on_set_charging_profile(
        self, connector_id: int, cs_charging_profiles: dict, **kwargs: Any
    ) -> call_result.SetChargingProfile:
        """Store the period-0 limit (unit W) of a TxProfile for the running transaction."""
        rejected = call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        accepted = call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)
        try:
            transaction_id, limit_kw = parse_tx_profile(connector_id, cs_charging_profiles)
        except ValueError as exc:
            log.warning("%s: SetChargingProfile rejected: %s", self.id, exc)
            return rejected
        tx = self.state.tx
        if tx is not None:
            if transaction_id is not None and transaction_id != tx.transaction_id:
                log.warning(
                    "%s: SetChargingProfile rejected: transactionId %s is not the running transaction %d",
                    self.id, transaction_id, tx.transaction_id,
                )
                return rejected
            self._advance(tx)  # integrate up to now at the old limit
            tx.limit_kw = limit_kw
            if tx.stop_reason is None:
                tx.power_kw = self._power_now(tx)
            log.info(
                "%s: limit %.0f W for tx %d -> drawing %.2f kW",
                self.id, limit_kw * W_PER_KW, tx.transaction_id, tx.power_kw,
            )
            return accepted
        if self.state.phase == PREPARING:
            # Our StartTransaction is in flight: the CSMS already knows the transactionId, this CP
            # does not yet. Keep the limit and apply it when the transaction starts.
            self.state.pending_limit = (transaction_id, limit_kw)
            log.info("%s: limit %.0f W stored until StartTransaction completes", self.id, limit_kw * W_PER_KW)
            return accepted
        log.warning("%s: SetChargingProfile rejected: no transaction in progress", self.id)
        return rejected

    @on(Action.remote_stop_transaction)
    def on_remote_stop_transaction(
        self, transaction_id: int, **kwargs: Any
    ) -> call_result.RemoteStopTransaction:
        """Stop the running transaction (reason Remote); the stop sequence runs in its own task."""
        tx = self.state.tx
        if tx is None or tx.transaction_id != transaction_id:
            log.warning(
                "%s: RemoteStopTransaction %s rejected: no such running transaction", self.id, transaction_id
            )
            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)
        self._advance(tx)
        if tx.stop_reason is None:
            self._stop(tx, Reason.remote)
        log.info("%s: RemoteStopTransaction %d accepted (%s)", self.id, transaction_id, tx.stop_reason)
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    # ------------------------------------------------------------------ transaction

    async def _plug_in_sequence(self, plug_in: PlugIn) -> None:
        """StatusNotification(Preparing) -> StartTransaction -> StatusNotification(Charging)."""
        log.info(
            "%s: plug-in %r: %.1f kWh, max %.1f kW, SoC %.1f%% -> %.1f%%, departs in %.2f h (simulated)",
            self.id, plug_in.vehicle_model, plug_in.battery_kwh, plug_in.max_kw,
            plug_in.soc_start * PERCENT, plug_in.soc_target * PERCENT, plug_in.hours_until_departure,
        )
        await self._status(ChargePointStatus.preparing)
        result = await self._call(
            call.StartTransaction(
                connector_id=CONNECTOR_ID,
                id_tag=self.id_tag,
                meter_start=0,  # the energy register restarts at 0 Wh for every transaction
                timestamp=self.clock.iso_now(),
            )
        )
        status = None if result is None else _id_tag_status(result.id_tag_info)
        if status != AuthorizationStatus.accepted:
            log.warning(
                "%s: StartTransaction not accepted (idTagInfo status %s); not charging", self.id, status
            )
            self.state.pending_limit = None
            await self._status(ChargePointStatus.available)
            self.state.phase = IDLE
            return

        tx = Transaction(
            transaction_id=int(result.transaction_id),
            vehicle_model=plug_in.vehicle_model,
            battery_kwh=plug_in.battery_kwh,
            max_kw=plug_in.max_kw,
            soc=plug_in.soc_start,
            soc_target=plug_in.soc_target,
            departure_sim_s=plug_in.hours_until_departure * SECONDS_PER_HOUR,
            limit_kw=self.charger_max_kw,  # no profile yet: the charger rating
            last_mono=time.monotonic(),
        )
        pending, self.state.pending_limit = self.state.pending_limit, None
        if pending is not None:
            pending_tid, pending_kw = pending
            if pending_tid is None or pending_tid == tx.transaction_id:
                tx.limit_kw = pending_kw
            else:
                log.warning(
                    "%s: dropping the stored limit for transactionId %s (started %d)",
                    self.id, pending_tid, tx.transaction_id,
                )
        tx.power_kw = self._power_now(tx)
        self.state.tx = tx
        self.state.phase = CHARGING
        log.info(
            "%s: transaction %d started, drawing %.2f kW (limit %.2f kW)",
            self.id, tx.transaction_id, tx.power_kw, tx.limit_kw,
        )
        await self._status(ChargePointStatus.charging)
        self._spawn(self._run_transaction(), "transaction")

    async def _fault_sequence(self) -> None:
        """Drop the connector's limit to 0 kW, then StatusNotification(Faulted).

        A running transaction is kept open and keeps reporting MeterValues, now at 0 kW: the car
        is still plugged in, the charge point simply cannot charge it. The CSMS stores the status
        and its orchestrator plans nothing for a faulted charger, which is what the fault-injection
        scenario shows. Nothing here clears the fault; the next transaction end does, with the
        usual Finishing -> Available.
        """
        tx = self.state.tx
        if tx is None:
            log.warning("%s: fault injected with no transaction running", self.id)
        else:
            self._advance(tx)  # integrate up to now at the old limit
            tx.limit_kw = 0.0
            if tx.stop_reason is None:
                tx.power_kw = self._power_now(tx)
            log.warning(
                "%s: fault injected on transaction %d -> drawing %.2f kW",
                self.id, tx.transaction_id, tx.power_kw,
            )
        await self._status(ChargePointStatus.faulted, FAULT_ERROR_CODE)

    async def _run_transaction(self) -> None:
        """Physics every PHYSICS_INTERVAL_S and MeterValues every METER_INTERVAL_S (real seconds)
        until a stop rule fires, then the stop sequence."""
        tx = self.state.tx
        if tx is None:
            return
        next_meter = time.monotonic() + METER_INTERVAL_S
        while True:
            self._advance(tx)
            if tx.stop_reason is not None:
                await self._finish(tx)
                return
            if time.monotonic() >= next_meter:
                await self._send_meter_values(tx, ReadingContext.sample_periodic)
                next_meter += METER_INTERVAL_S
                if next_meter <= time.monotonic():  # fell behind (slow CSMS): do not burst
                    next_meter = time.monotonic() + METER_INTERVAL_S
            await asyncio.sleep(max(0.0, min(PHYSICS_INTERVAL_S, next_meter - time.monotonic())))

    def _power_now(self, tx: Transaction) -> float:
        """Power drawn now: min(limit, what the vehicle accepts at its SOC)."""
        return min(tx.limit_kw, acceptance_kw(tx.soc, tx.max_kw))

    def _stop(self, tx: Transaction, reason: Reason) -> None:
        tx.stop_reason = reason
        tx.power_kw = 0.0

    def _advance(self, tx: Transaction) -> None:
        """Integrate the battery from ``tx.last_mono`` to now, applying the stop rules.

        Steps are at most PHYSICS_INTERVAL_S real seconds, each with
        dt_hours = real_elapsed * TIME_SCALE / 3600; the power is re-evaluated after every step.
        A step in which the car departs or reaches soc_target is cut short at that moment, so
        neither rule overshoots by up to a whole step when TIME_SCALE is large.
        Pure computation, so it is safe to call from an @on handler.
        """
        now = time.monotonic()
        remaining_s = now - tx.last_mono
        tx.last_mono = now
        time_scale = self.clock.time_scale
        while remaining_s > 0 and tx.stop_reason is None:
            dt_real_s = min(remaining_s, PHYSICS_INTERVAL_S)
            remaining_s -= dt_real_s
            dt_sim_s = dt_real_s * time_scale
            to_departure_s = tx.departure_sim_s - tx.sim_elapsed_s
            departs = dt_sim_s >= to_departure_s
            if departs:  # the car leaves during this step: it charges only until then
                dt_sim_s = max(0.0, to_departure_s)
            dt_hours = dt_sim_s / SECONDS_PER_HOUR
            soc = step(tx.soc, tx.battery_kwh, tx.power_kw, dt_hours)
            reached = soc >= tx.soc_target
            if reached:  # the target is reached during this step: charging stops right there
                dt_hours *= _fraction_to_reach(tx.soc, tx.soc_target, tx.battery_kwh, tx.power_kw, dt_hours)
                soc = step(tx.soc, tx.battery_kwh, tx.power_kw, dt_hours)
                dt_sim_s = dt_hours * SECONDS_PER_HOUR
            tx.soc = soc
            tx.energy_wh += tx.power_kw * dt_hours * WH_PER_KWH
            tx.sim_elapsed_s += dt_sim_s
            if reached:
                self._stop(tx, Reason.local)
            elif departs:
                tx.sim_elapsed_s = tx.departure_sim_s
                self._stop(tx, Reason.ev_disconnected)
            else:
                tx.power_kw = self._power_now(tx)

    async def _finish(self, tx: Transaction) -> None:
        """Last MeterValues (Transaction.End) -> StopTransaction -> Finishing -> Available."""
        self.state.phase = FINISHING
        log.info(
            "%s: stopping transaction %d (%s): SoC %.1f%%, %.3f kWh, %.2f h simulated",
            self.id, tx.transaction_id, tx.stop_reason, tx.soc * PERCENT,
            tx.energy_wh / WH_PER_KWH, tx.sim_elapsed_s / SECONDS_PER_HOUR,
        )
        await self._send_meter_values(tx, ReadingContext.transaction_end)
        await self._call(
            call.StopTransaction(
                meter_stop=round(tx.energy_wh),
                timestamp=self.clock.iso_now(),
                transaction_id=tx.transaction_id,
                reason=tx.stop_reason,
                id_tag=self.id_tag,
            )
        )
        self.state.tx = None
        await self._status(ChargePointStatus.finishing)
        await self._status(ChargePointStatus.available)
        self.state.phase = IDLE
