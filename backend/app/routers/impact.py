"""Impact and optimizer-control endpoints (Phase 4).

    GET  /api/impact/summary    -> {"co2_saved_kg", "cost_saved_inr", "sessions_on_time",
                                    "total_sessions"}, exactly these four keys, from
                                    ``orchestrator.accounting.impact_summary`` with the
                                    orchestrator's latest plans and the last tick's unmet energy.
                                    A ProviderError (no forecast cached to price an active
                                    session's remaining plan) is HTTP 503 with its message, as
                                    in the grid endpoints.
    POST /api/optimizer/weights -> body {"alpha" >= 0, "beta" >= 0} (carbon and cost weights of
                                    the objective); sets them and runs a tick at once, awaited,
                                    so the answer already reflects the new weights:
                                    {"alpha", "beta", "tick"} where "tick" is that tick's record.

The POST body is read with ``parse_json_body``, so it works without a Content-Type header.

The summary runs in a worker thread (the orchestrator's ``get_latest_schedules()`` and the
accounting's database reads), so a slow database cannot stall the event loop the CSMS and the
tick scheduler run on. The weights tick is awaited on that loop.
"""
import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.clock import clock
from app.db import SessionLocal
from app.orchestrator import accounting
from app.orchestrator import loop as orchestrator
from app.providers import ProviderError
from app.routers import parse_json_body
from app.schemas import ImpactSummaryOut, WeightsRequest

logger = logging.getLogger("greencharge.routers.impact")

router = APIRouter(prefix="/api", tags=["impact"])

WEIGHTS_TICK_REASON = "weights"


def _compute_impact(now: datetime, last_unmet_kwh: dict[int, float]) -> dict[str, Any]:
    """accounting.impact_summary with the latest plans. Blocking; run in a worker thread."""
    latest_schedules = orchestrator.get_latest_schedules()
    with SessionLocal() as db:
        return accounting.impact_summary(db, now, latest_schedules, last_unmet_kwh)


@router.get("/impact/summary", response_model=ImpactSummaryOut)
async def impact_summary() -> dict[str, Any]:
    last_tick = orchestrator.state.last_tick or {}
    last_unmet_kwh = dict(last_tick.get("unmet_kwh") or {})
    try:
        return await asyncio.to_thread(_compute_impact, clock.now(), last_unmet_kwh)
    except ProviderError as exc:
        logger.warning("Impact summary unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/optimizer/weights")
async def optimizer_weights(request: Request) -> dict[str, Any]:
    body = await parse_json_body(request, WeightsRequest)
    orchestrator.set_weights(body.alpha, body.beta)
    # The weights now in effect, read before the tick: a request arriving while it runs may
    # change them again.
    alpha, beta = orchestrator.state.alpha, orchestrator.state.beta
    tick = await orchestrator.tick(WEIGHTS_TICK_REASON)
    return {"alpha": alpha, "beta": beta, "tick": tick}
