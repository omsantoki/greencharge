"""Site endpoints.

    GET /api/sites                    -> list[SiteOut], each site with its chargers nested and
                                         ordered by charger id (Phase 1)
    GET /api/sites/{site_id}/load-curve -> LoadCurveOut (Phase 4): the site's optimized vs
                                         baseline aggregate kW over 96 slots, from
                                         ``orchestrator.baseline.site_load_curve`` with the
                                         orchestrator's latest plans. 404 for an unknown site.

The load curve runs in a worker thread (the orchestrator's ``get_latest_schedules()``, the
database reads and the baseline simulation), so neither a slow database nor the computation
stalls the event loop the CSMS and the tick scheduler run on.
"""
import asyncio
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.clock import clock
from app.db import SessionLocal, get_db
from app.models import Site
from app.orchestrator import baseline
from app.orchestrator import loop as orchestrator
from app.schemas import LoadCurveOut, SiteOut

router = APIRouter(prefix="/api", tags=["sites"])


# Plain `def` (not async): the ORM session is blocking, so FastAPI runs this in its threadpool.
@router.get("/sites", response_model=list[SiteOut])
def list_sites(db: Annotated[DbSession, Depends(get_db)]) -> list[Site]:
    # Chargers are eager-loaded in one extra query; their order comes from the
    # relationship's order_by (Charger.id).
    stmt = select(Site).options(selectinload(Site.chargers)).order_by(Site.id)
    return list(db.scalars(stmt).all())


def _compute_load_curve(site_id: int, now: datetime) -> dict[str, Any] | None:
    """The site's load curve, or None when there is no such site. Blocking; run in a thread."""
    latest_schedules = orchestrator.get_latest_schedules()
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            return None
        return baseline.site_load_curve(db, site, now, latest_schedules)


@router.get("/sites/{site_id}/load-curve", response_model=LoadCurveOut)
async def load_curve(site_id: int) -> dict[str, Any]:
    curve = await asyncio.to_thread(_compute_load_curve, site_id, clock.now())
    if curve is None:
        raise HTTPException(status_code=404, detail=f"Site {site_id} not found")
    return curve
