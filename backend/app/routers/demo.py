"""Demo control endpoints (Phase 4; the Phase 8 one-click buttons use them too).

    POST /api/demo/reset            -> cancel a running scenario, stop every active session with
                                       RemoteStopTransaction (waiting at most 3 s for the
                                       StopTransactions), then TRUNCATE meter_values, schedules
                                       and sessions (RESTART IDENTITY) and clear the orchestrator
                                       and OCPP in-memory state. Seed data and grid_data survive.
                                       {"reset": true, "sessions": [{"session_id", "ocpp_id",
                                       "remote_stop", "stopped"}], "waited_s", "truncated",
                                       "cancelled_scenario"}
    POST /api/demo/scenario/{name}  -> start the scenario in a background task
                                       {"scenario", "started": true, "steps"}
                                       404 unknown scenario; 409 one is running or a reset is
                                       in progress
    GET  /api/demo/status           -> {"scenario", "running", "step", "total_steps", "events",
                                       "error"} of the latest scenario run

The two POSTs take no body (any body is ignored). The work itself lives in ``app.scenarios``;
these handlers are ``async`` because it runs on the event loop that owns the OCPP registry.
Errors: a database failure during the reset -> 503 (never a 500).
"""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app import scenarios

logger = logging.getLogger("greencharge.routers.demo")

router = APIRouter(prefix="/api", tags=["demo"])


@router.post("/demo/reset")
async def demo_reset() -> dict[str, Any]:
    try:
        return await scenarios.reset_demo()
    except SQLAlchemyError as exc:
        logger.exception("Demo reset failed")
        raise HTTPException(
            status_code=503, detail=f"Demo reset failed: {scenarios.describe_error(exc)}"
        ) from None


@router.post("/demo/scenario/{name}")
async def demo_scenario(name: str) -> dict[str, Any]:
    try:
        scenario = scenarios.start_scenario(name)
    except scenarios.UnknownScenarioError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except scenarios.ScenarioBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"scenario": scenario.name, "started": True, "steps": len(scenario.events)}


@router.get("/demo/status")
async def demo_status() -> dict[str, Any]:
    return scenarios.scenario_status()
