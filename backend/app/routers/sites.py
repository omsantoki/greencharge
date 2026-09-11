"""Site endpoints (Phase 1).

    GET /api/sites -> list[SiteOut], each site with its chargers nested and ordered by charger id
"""
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import Site
from app.schemas import SiteOut

router = APIRouter(prefix="/api", tags=["sites"])


# Plain `def` (not async): the ORM session is blocking, so FastAPI runs this in its threadpool.
@router.get("/sites", response_model=list[SiteOut])
def list_sites(db: Annotated[DbSession, Depends(get_db)]) -> list[Site]:
    # Chargers are eager-loaded in one extra query; their order comes from the
    # relationship's order_by (Charger.id).
    stmt = select(Site).options(selectinload(Site.chargers)).order_by(Site.id)
    return list(db.scalars(stmt).all())
