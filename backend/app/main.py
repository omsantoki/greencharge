"""GreenCharge FastAPI application.

/health (liveness plus DB/Redis reachability), and the Phase 1 data-layer routers: grid carbon
intensity and tariff (`app.routers.grid`) and sites with nested chargers (`app.routers.sites`).
Tables are created idempotently at startup.
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI

from app.config import settings
from app.db import Base, db_ok, engine
from app.routers import grid, sites

logger = logging.getLogger("greencharge.main")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Importing the models registers every table on Base.metadata before create_all.
    from app import models  # noqa: F401

    # create_all is idempotent. A failure (e.g. Postgres down) is logged, not raised, so the app
    # still starts and /health can report db=false.
    try:
        Base.metadata.create_all(engine)
    except Exception as exc:
        logger.warning("Could not create database tables at startup: %s", exc)
    yield


app = FastAPI(title="GreenCharge", lifespan=lifespan)
app.include_router(grid.router)
app.include_router(sites.router)


def redis_ok() -> bool:
    """Return True if Redis answers PING, False on any error."""
    client = None
    try:
        client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        return bool(client.ping())
    except Exception as exc:
        logger.warning("Redis check failed: %s", exc)
        return False
    finally:
        if client is not None:
            client.close()


# Plain `def` (not async): the DB and Redis checks are blocking calls, so FastAPI runs
# this endpoint in its threadpool instead of on the event loop.
@app.get("/health")
def health() -> dict[str, bool | str]:
    # "status" is liveness and always "ok"; db/redis report reachability.
    return {"status": "ok", "db": db_ok(), "redis": redis_ok()}
