"""Debug and operator endpoints for the OCPP layer (Phase 2).

    POST /api/debug/plug-in      -> simulate a car plugging into a charger; returns once the charge
                                    point's StartTransaction has created the Session
                                    {"session_id", "transaction_id", "charger_id", "ocpp_id"}
    POST /api/debug/set-limit    -> a manual power limit (W) for the charger's active session, pushed
                                    with SetChargingProfile {"status", "session_id", "limit_w"}
    GET  /api/ocpp/log?limit=50  -> the most recent raw OCPP-J frames, newest first
    GET  /api/clock              -> the simulation clock {"now", "time_scale"}

Both POST bodies are read with ``parse_json_body``, so they work without a Content-Type header.

Plug-in relay: the endpoint stores the vehicle parameters in ``registry.pending_plugins`` together
with a future in ``registry.session_waiters``, then sends the charge point an OCPP DataTransfer
(vendor "GreenCharge", message "SimPlugIn"). The simulated charge point answers it and runs
StatusNotification(Preparing) -> StartTransaction -> StatusNotification(Charging). The CSMS
StartTransaction handler creates the Session from the pending parameters and resolves the future
with its id. No meter values are written here; they only ever come from the charge point's
MeterValues messages.

Expected failures map to HTTP errors, never to a 500:
  404  unknown charger or vehicle model
  409  charger not connected, busy (active session, plug-in in progress, or the charge point
       refused), no active session to limit, or the charge point disconnected during the call
  422  invalid body
  502  the charge point answered with an OCPP error, an unexpected status, or the call failed
  504  the charge point did not answer in time, or no StartTransaction arrived in time

These endpoints are ``async``: the OCPP registry and its futures belong to the event loop the
CSMS runs on, so they are only touched from that loop. Their database reads are small inline
calls.
"""
import asyncio
import logging
from collections.abc import Awaitable
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from ocpp.v16.enums import ChargingProfileStatus, DataTransferStatus
from sqlalchemy import select
from websockets.exceptions import ConnectionClosed

from app.clock import clock
from app.db import SessionLocal
from app.models import Charger, Session
from app.ocpp import registry
from app.ocpp.handlers import STATUS_CALL_ERROR, STATUS_ERROR, STATUS_TIMEOUT
from app.routers import parse_json_body
from app.schemas import PlugInRequest, SetLimitRequest
from app.seed import get_vehicle, load_vehicles

logger = logging.getLogger("greencharge.routers.debug")

router = APIRouter(prefix="/api", tags=["debug"])

# Real-time budgets from the implementation contract (section 5b).
PLUG_IN_TIMEOUT_S = 15.0  # plug-in: DataTransfer answer + StartTransaction, in total
SET_LIMIT_SESSION_WAIT_S = 10.0  # set-limit: how long to wait for an active session to appear
SESSION_POLL_INTERVAL_S = 0.25  # set-limit: how often to look for that session meanwhile

SESSION_ACTIVE = "active"  # Session.status of a session still in progress


def _charger_label(charger: Charger) -> str:
    return f"Charger {charger.id} ({charger.ocpp_id})"


def _load_charger(charger_id: int) -> Charger:
    """The charger row, or HTTP 404."""
    with SessionLocal() as db:
        charger = db.get(Charger, charger_id)
    if charger is None:
        raise HTTPException(status_code=404, detail=f"Charger {charger_id} not found")
    return charger


def _connection(charger: Charger) -> registry.ChargePointConnection:
    """The charger's live OCPP connection, or HTTP 409 when it is not connected."""
    conn = registry.get(charger.ocpp_id)
    if conn is None:
        raise HTTPException(
            status_code=409,
            detail=f"{_charger_label(charger)} is not connected to the CSMS",
        )
    return conn


def _active_session(charger_id: int) -> Session | None:
    """The charger's newest active session, if any."""
    with SessionLocal() as db:
        return db.scalars(
            select(Session)
            .where(Session.charger_id == charger_id, Session.status == SESSION_ACTIVE)
            .order_by(Session.id.desc())
        ).first()


async def _wait_for_active_session(charger_id: int, timeout_s: float) -> Session | None:
    """Poll for an active session that already has its OCPP transaction id, for up to timeout_s."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        session = _active_session(charger_id)
        if session is not None and session.ocpp_transaction_id is not None:
            return session
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(SESSION_POLL_INTERVAL_S, remaining))


async def _call_charge_point(
    conn: registry.ChargePointConnection,
    action: str,
    call: Awaitable[Any],
    timeout_s: float | None = None,
) -> str:
    """Await one outbound call of ``CentralSystemChargePoint`` and return the charge point's
    status string ("Accepted", "Rejected", ...).

    Those methods do not raise; when the charge point gave no status they return
    ``STATUS_TIMEOUT``, ``STATUS_CALL_ERROR`` or ``STATUS_ERROR`` (the call could not be made,
    e.g. the connection closed). Without ``timeout_s`` the connection's response timeout
    applies. Failures become HTTP errors: no answer in time -> 504; charger disconnected -> 409;
    a CALLERROR answer or any other failure -> 502.
    """
    ocpp_id = conn.ocpp_id
    try:
        result = await (call if timeout_s is None else asyncio.wait_for(call, timeout_s))
    except TimeoutError as exc:  # asyncio.TimeoutError is TimeoutError on Python 3.11
        waited = "in time" if timeout_s is None else f"within {timeout_s:g} s"
        raise HTTPException(
            status_code=504, detail=f"Charger {ocpp_id} did not answer {action} {waited}"
        ) from exc
    except ConnectionClosed as exc:
        raise HTTPException(
            status_code=409, detail=f"Charger {ocpp_id} disconnected during {action}"
        ) from exc
    except Exception as exc:
        logger.exception("%s to charger %s failed", action, ocpp_id)
        raise HTTPException(
            status_code=502, detail=f"{action} to charger {ocpp_id} failed: {exc}"
        ) from exc
    # Status enums are StrEnums; compare and report the plain value.
    status = str(getattr(result, "value", result))
    if status == STATUS_TIMEOUT:
        raise HTTPException(
            status_code=504, detail=f"Charger {ocpp_id} did not answer {action} in time"
        )
    if status == STATUS_CALL_ERROR or result is None:
        raise HTTPException(
            status_code=502,
            detail=f"Charger {ocpp_id} answered {action} with an OCPP CALLERROR",
        )
    if status == STATUS_ERROR:
        if registry.get(ocpp_id) is not conn:
            raise HTTPException(
                status_code=409, detail=f"Charger {ocpp_id} disconnected during {action}"
            )
        raise HTTPException(
            status_code=502,
            detail=f"{action} to charger {ocpp_id} could not be made (details in the server log)",
        )
    return status


@router.post("/debug/plug-in")
async def plug_in(request: Request) -> dict[str, Any]:
    body = await parse_json_body(request, PlugInRequest)

    vehicle = get_vehicle(body.vehicle_model)
    if vehicle is None:
        known = ", ".join(v["model"] for v in load_vehicles())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown vehicle_model {body.vehicle_model!r}; known models: {known}",
        )
    try:
        # The CSMS computes deadline = plug-in time + hours_until_departure (simulated hours).
        clock.now() + timedelta(hours=body.hours_until_departure)
    except OverflowError:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["hours_until_departure"],
                    "msg": "Value error, the departure time is beyond the representable date range",
                    "input": body.hours_until_departure,
                }
            ],
        ) from None

    charger = _load_charger(body.charger_id)
    conn = _connection(charger)
    ocpp_id = charger.ocpp_id

    active = _active_session(charger.id)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{_charger_label(charger)} already has active session {active.id}",
        )
    # No await between this check and registering the pending plug-in below, so two concurrent
    # requests for one charger cannot both get past it.
    if ocpp_id in registry.pending_plugins or ocpp_id in registry.session_waiters:
        raise HTTPException(
            status_code=409,
            detail=f"A plug-in is already in progress on {_charger_label(charger)}",
        )

    params = {
        "vehicle_model": vehicle["model"],
        "battery_kwh": float(vehicle["battery_kwh"]),
        "max_kw": min(float(vehicle["max_ac_kw"]), float(charger.max_power_kw)),
        "soc_start": body.soc_start,
        "soc_target": body.soc_target,
        "hours_until_departure": body.hours_until_departure,
    }
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future = loop.create_future()
    registry.pending_plugins[ocpp_id] = params
    registry.session_waiters[ocpp_id] = waiter
    deadline = loop.time() + PLUG_IN_TIMEOUT_S
    try:
        status = await _call_charge_point(
            conn,
            "the plug-in DataTransfer",
            conn.cp.send_plug_in(params),
            timeout_s=PLUG_IN_TIMEOUT_S,
        )
        if status == DataTransferStatus.rejected:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{_charger_label(charger)} rejected the plug-in (status {status!r}); "
                    "the simulated charge point does this while a car is already plugged in"
                ),
            )
        if status != DataTransferStatus.accepted:
            raise HTTPException(
                status_code=502,
                detail=f"{_charger_label(charger)} answered the plug-in DataTransfer with status {status!r}",
            )
        try:
            # shield: a timeout here must not cancel the future the CSMS handler may resolve.
            session_id = await asyncio.wait_for(
                asyncio.shield(waiter), timeout=max(0.0, deadline - loop.time())
            )
        except TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"{_charger_label(charger)} accepted the plug-in but no StartTransaction "
                    f"created a session within {PLUG_IN_TIMEOUT_S:g} s"
                ),
            ) from None
    finally:
        # Whatever happened, this request's plug-in is no longer pending. The StartTransaction
        # handler may already have taken these entries; never remove a newer request's entries.
        if registry.pending_plugins.get(ocpp_id) is params:
            del registry.pending_plugins[ocpp_id]
        if registry.session_waiters.get(ocpp_id) is waiter:
            del registry.session_waiters[ocpp_id]

    with SessionLocal() as db:
        session = db.get(Session, session_id)
    transaction_id = session.ocpp_transaction_id if session is not None else None
    logger.info(
        "Plug-in on %s: session %s (transaction %s), %s, max %.1f kW",
        ocpp_id, session_id, transaction_id, params["vehicle_model"], params["max_kw"],
    )
    return {
        "session_id": session_id,
        "transaction_id": transaction_id,
        "charger_id": charger.id,
        "ocpp_id": ocpp_id,
    }


@router.post("/debug/set-limit")
async def set_limit(request: Request) -> dict[str, Any]:
    body = await parse_json_body(request, SetLimitRequest)

    charger = _load_charger(body.charger_id)
    _connection(charger)  # fail fast when the charger is offline
    session = await _wait_for_active_session(charger.id, SET_LIMIT_SESSION_WAIT_S)
    if session is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{_charger_label(charger)} has no active session "
                f"(waited {SET_LIMIT_SESSION_WAIT_S:g} s)"
            ),
        )
    conn = _connection(charger)  # it may have disconnected while we waited

    # Record the operator's limit first so the orchestrator treats this session as manually
    # limited from now on; undo it below if the charge point does not accept the profile.
    had_previous = session.id in registry.manual_limits_w
    previous = registry.manual_limits_w.get(session.id)
    registry.manual_limits_w[session.id] = body.limit_w
    accepted = False
    try:
        status = await _call_charge_point(
            conn,
            "SetChargingProfile",
            conn.cp.set_charging_profile(session.ocpp_transaction_id, body.limit_w),
        )
        if status == ChargingProfileStatus.rejected:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{_charger_label(charger)} rejected the charging profile for session "
                    f"{session.id} (status {status!r})"
                ),
            )
        if status != ChargingProfileStatus.accepted:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"{_charger_label(charger)} answered SetChargingProfile with status {status!r}"
                ),
            )
        accepted = True
    finally:
        # Undo only our own write: a concurrent request may have set a newer limit meanwhile.
        if not accepted and registry.manual_limits_w.get(session.id) == body.limit_w:
            if had_previous:
                registry.manual_limits_w[session.id] = previous
            else:
                registry.manual_limits_w.pop(session.id, None)

    logger.info(
        "Manual limit %g W set on %s for session %s", body.limit_w, charger.ocpp_id, session.id
    )
    return {"status": status, "session_id": session.id, "limit_w": body.limit_w}


@router.get("/ocpp/log")
async def ocpp_log(limit: Annotated[int, Query(ge=1)] = 50) -> list[dict[str, Any]]:
    """Up to ``limit`` raw OCPP-J frames, newest first: {"ts", "direction", "ocpp_id", "frame"}.

    ``ts`` is simulation time; the log keeps only the most recent frames, so a larger limit
    simply returns everything that is kept.
    """
    return registry.recent_frames(limit)


@router.get("/clock")
async def sim_clock() -> dict[str, Any]:
    """The simulation clock: ``now`` (ISO-8601, UTC) and ``time_scale`` (sim s per real s)."""
    return {"now": clock.now().isoformat(), "time_scale": clock.time_scale}
