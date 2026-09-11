"""Charging-session endpoints.

Phase 2:

    GET /api/sessions/{session_id}/meter-values -> NDJSON (application/x-ndjson): one JSON object
        {"ts", "power_kw", "energy_kwh", "soc"} per line, oldest first, so `| tail -1` is the
        newest reading. 404 if the session does not exist; an existing session with no readings
        yet returns an empty body.

The rows are exactly what the CSMS MeterValues handler stored from the charge point's OCPP
MeterValues messages: ``ts`` is the simulation time at receipt (ISO-8601, UTC), ``power_kw`` the
instantaneous power, ``energy_kwh`` the cumulative energy of the session and ``soc`` the state of
charge (0.0-1.0). Readings are returned in the order they were received (by row id). That is
also ``ts`` order, unless the simulation clock is moved backwards during a session; then the
last line is still the most recent reading.

Phase 4 (orchestration):

    GET  /api/sessions/active                -> list[ActiveSessionOut], by session id: every
        Session column plus ``ocpp_id``, ``manual_limit_w`` (the operator limit in W, or null),
        ``projected_unmet_kwh`` (the last tick's unmet energy for the session, or null),
        ``on_time`` (the last tick left no unmet energy for it) and ``schedule``, the latest plan
        as [{"slot_start", "power_kw"}] (96 slots; [] before the first plan).
    GET  /api/sessions/{session_id}/schedule -> {"session_id", "computed_at", "slots":
        [{"slot_start", "power_kw"} x 96]}: the latest plan. 404 when the session does not exist
        or has no plan yet.
    POST /api/sessions/{session_id}/override -> {"session_id", "limit_w", "status"}: "charge at
        max now". Sets the session's manual limit to max_charge_kw x 1000 W, pushes it to the
        charge point at once with SetChargingProfile (awaited), then asks the orchestrator for a
        re-tick, which re-sends the limit every tick from then on. ``status`` is the charge
        point's answer ("Accepted"). The body is optional (empty or a JSON object).

Override errors (never a 500):
  404  unknown session
  409  session not active, charger not connected (or it disconnected during the call), or the
       charge point rejected the profile
  422  a body that is not a JSON object
  502  the charge point answered with an OCPP CALLERROR, or the call could not be made
  504  the charge point did not answer in time
When the charge point does not accept the profile, the manual limit this request set is removed
again, so the orchestrator keeps planning the session as before.

These endpoints are ``async`` and run on the event loop the CSMS and the tick scheduler share:
the OCPP connections and ``registry.manual_limits_w`` belong to that loop. Everything that
queries the database, including the orchestrator's ``get_latest_schedules()``, runs in a worker
thread, so a slow or unreachable database cannot stall the loop.
"""
import asyncio
import json
import logging
from datetime import timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from ocpp.v16.enums import ChargingProfileStatus
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session as DbSession

from app.db import SessionLocal, get_db
from app.models import Charger, MeterValue, Session
from app.ocpp import registry
from app.ocpp.handlers import STATUS_CALL_ERROR, STATUS_ERROR, STATUS_TIMEOUT
from app.orchestrator import loop as orchestrator
from app.routers import parse_json_body
from app.schemas import ActiveSessionOut, OverrideRequest, SessionScheduleOut

logger = logging.getLogger("greencharge.routers.sessions")

router = APIRouter(prefix="/api", tags=["sessions"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"

SESSION_ACTIVE = "active"  # Session.status of a session still in progress
W_PER_KW = 1000.0
OVERRIDE_TICK_REASON = "override"

# Attribute names of every mapped Session column (id, charger_id, ..., status).
_SESSION_COLUMNS = tuple(attr.key for attr in inspect(Session).column_attrs)


# Plain `def` (not async): the ORM session is blocking, so FastAPI runs this in its threadpool.
@router.get(
    "/sessions/{session_id}/meter-values",
    response_class=Response,
    responses={200: {"content": {NDJSON_MEDIA_TYPE: {}}, "description": "One reading per line"}},
)
def session_meter_values(
    session_id: int, db: Annotated[DbSession, Depends(get_db)]
) -> Response:
    if db.get(Session, session_id) is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    rows = db.execute(
        select(MeterValue.ts, MeterValue.power_kw, MeterValue.energy_kwh, MeterValue.soc)
        .where(MeterValue.session_id == session_id)
        .order_by(MeterValue.id)
    ).all()
    lines = [
        json.dumps(
            {
                "ts": row.ts.astimezone(timezone.utc).isoformat(),
                "power_kw": row.power_kw,
                "energy_kwh": row.energy_kwh,
                "soc": row.soc,
            }
        )
        + "\n"
        for row in rows
    ]
    return Response(content="".join(lines), media_type=NDJSON_MEDIA_TYPE)


# --------------------------------------------------------------------------------------------
# Phase 4
# --------------------------------------------------------------------------------------------


def _slots_out(plan: dict | None) -> list[dict[str, Any]]:
    """An orchestrator plan {"computed_at", "slots": [(slot_start, kW), ...]} as API slots."""
    if not plan:
        return []
    return [{"slot_start": slot_start, "power_kw": kw} for slot_start, kw in plan["slots"]]


def _load_active_sessions() -> tuple[list[dict[str, Any]], dict[int, dict]]:
    """Every active session as {column: value, ..., "ocpp_id"} (by session id), and the
    orchestrator's latest plans. Blocking (database); run in a worker thread."""
    plans = orchestrator.get_latest_schedules()
    with SessionLocal() as db:
        rows = db.execute(
            select(Session, Charger.ocpp_id)
            .join(Charger, Session.charger_id == Charger.id)
            .where(Session.status == SESSION_ACTIVE)
            .order_by(Session.id)
        ).all()
        sessions = [
            {**{key: getattr(session, key) for key in _SESSION_COLUMNS}, "ocpp_id": ocpp_id}
            for session, ocpp_id in rows
        ]
    return sessions, plans


@router.get("/sessions/active", response_model=list[ActiveSessionOut])
async def active_sessions() -> list[dict[str, Any]]:
    last_tick = orchestrator.state.last_tick or {}
    unmet_kwh = dict(last_tick.get("unmet_kwh") or {})
    manual_limits_w = dict(registry.manual_limits_w)

    sessions, plans = await asyncio.to_thread(_load_active_sessions)

    out = []
    for row in sessions:
        session_id = row["id"]
        unmet = unmet_kwh.get(session_id)
        out.append(
            {
                **row,
                "manual_limit_w": manual_limits_w.get(session_id),
                "projected_unmet_kwh": unmet,
                # Same rule as the impact summary for an active session: unmet == 0 (or none).
                "on_time": (0.0 if unmet is None else unmet) == 0.0,
                "schedule": _slots_out(plans.get(session_id)),
            }
        )
    return out


def _load_plan(session_id: int) -> tuple[bool, dict | None]:
    """(whether the session exists, its latest plan or None). The orchestrator's latest plans
    cover finished sessions too (from the schedules table). Blocking; run in a worker thread."""
    plan = orchestrator.get_latest_schedules().get(session_id)
    if plan is not None:
        return True, plan
    with SessionLocal() as db:
        return db.get(Session, session_id) is not None, None


@router.get("/sessions/{session_id}/schedule", response_model=SessionScheduleOut)
async def session_schedule(session_id: int) -> dict[str, Any]:
    exists, plan = await asyncio.to_thread(_load_plan, session_id)
    if not exists:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} has no schedule yet")
    return {
        "session_id": session_id,
        "computed_at": plan["computed_at"],
        "slots": _slots_out(plan),
    }


def _db_session_and_ocpp_id(session_id: int) -> tuple[Session, str] | None:
    """The session row and its charger's ocpp_id, or None when there is no such session."""
    with SessionLocal() as db:
        row = db.execute(
            select(Session, Charger.ocpp_id)
            .join(Charger, Session.charger_id == Charger.id)
            .where(Session.id == session_id)
        ).first()
    return None if row is None else (row[0], row[1])


def _raise_unless_accepted(
    status: str, conn: registry.ChargePointConnection, session_id: int
) -> None:
    """Map a SetChargingProfile answer that is not "Accepted" to an HTTP error."""
    ocpp_id = conn.ocpp_id
    if status == ChargingProfileStatus.accepted:
        return
    if status == ChargingProfileStatus.rejected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Charger {ocpp_id} rejected the override profile for session {session_id} "
                f"(status {status!r})"
            ),
        )
    if status == STATUS_TIMEOUT:
        raise HTTPException(
            status_code=504, detail=f"Charger {ocpp_id} did not answer SetChargingProfile in time"
        )
    if status == STATUS_CALL_ERROR:
        raise HTTPException(
            status_code=502,
            detail=f"Charger {ocpp_id} answered SetChargingProfile with an OCPP CALLERROR",
        )
    if status == STATUS_ERROR and registry.get(ocpp_id) is not conn:
        raise HTTPException(
            status_code=409, detail=f"Charger {ocpp_id} disconnected during SetChargingProfile"
        )
    if status == STATUS_ERROR:
        raise HTTPException(
            status_code=502,
            detail=(
                f"SetChargingProfile to charger {ocpp_id} could not be made "
                "(details in the server log)"
            ),
        )
    raise HTTPException(
        status_code=502,
        detail=f"Charger {ocpp_id} answered SetChargingProfile with status {status!r}",
    )


@router.post("/sessions/{session_id}/override")
async def override_session(session_id: int, request: Request) -> dict[str, Any]:
    # No parameters: an empty body is fine; anything else must be a JSON object.
    if (await request.body()).strip():
        await parse_json_body(request, OverrideRequest)

    found = await asyncio.to_thread(_db_session_and_ocpp_id, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    session, ocpp_id = found
    if session.status != SESSION_ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"Session {session_id} is not active (status {session.status!r})",
        )
    if session.ocpp_transaction_id is None:
        raise HTTPException(
            status_code=409, detail=f"Session {session_id} has no OCPP transaction yet"
        )
    conn = registry.get(ocpp_id)
    if conn is None:
        raise HTTPException(
            status_code=409,
            detail=f"Charger {ocpp_id} of session {session_id} is not connected to the CSMS",
        )

    limit_w = session.max_charge_kw * W_PER_KW
    # Record the limit first, so a tick running meanwhile already treats the session as manually
    # limited; undo only this request's own write if the charge point does not accept it.
    had_previous = session_id in registry.manual_limits_w
    previous = registry.manual_limits_w.get(session_id)
    registry.manual_limits_w[session_id] = limit_w
    accepted = False
    try:
        # set_charging_profile never raises; it returns the charge point's status string.
        status = await conn.cp.set_charging_profile(session.ocpp_transaction_id, limit_w)
        _raise_unless_accepted(status, conn, session_id)
        accepted = True
    finally:
        if not accepted and registry.manual_limits_w.get(session_id) == limit_w:
            if had_previous:
                registry.manual_limits_w[session_id] = previous
            else:
                registry.manual_limits_w.pop(session_id, None)

    orchestrator.request_tick(OVERRIDE_TICK_REASON)
    logger.info(
        "Override on %s: session %d charges at max now (%g W)", ocpp_id, session_id, limit_w
    )
    return {"session_id": session_id, "limit_w": limit_w, "status": str(status)}
