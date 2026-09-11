"""Charging-session endpoints (Phase 2).

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
"""
import json
from datetime import timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models import MeterValue, Session

router = APIRouter(prefix="/api", tags=["sessions"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"


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
