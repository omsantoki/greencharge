"""GreenCharge FastAPI application.

- /health (liveness plus DB/Redis reachability).
- Phase 1 data-layer routers: grid carbon intensity and tariff (`app.routers.grid`) and sites
  with nested chargers (`app.routers.sites`).
- Phase 2 OCPP layer: the CSMS (OCPP 1.6J WebSocket server on `settings.ocpp_port`) runs IN THIS
  PROCESS, on the same event loop as the API. The lifespan starts it after the tables are
  created and stops it at shutdown; if it cannot bind its port the error is logged and the HTTP
  API keeps running without it. Routers: debug/operator endpoints (`app.routers.debug`) and
  session meter values (`app.routers.sessions`).

Tables are created idempotently at startup.
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI

from app.config import settings
from app.db import Base, db_ok, engine
from app.ocpp.csms import start_csms, stop_csms
from app.routers import debug, grid, sessions, sites

logger = logging.getLogger("greencharge.main")

# uvicorn configures only its own loggers. Without a handler here the app's INFO records (charge
# points connecting, plug-ins, transactions) would be dropped, and its warnings and errors would
# reach stderr without a level or logger name. Only the "greencharge" tree is configured, so the
# ocpp library's per-frame INFO logging stays off (the frames are in GET /api/ocpp/log).
_app_logger = logging.getLogger("greencharge")
if not _app_logger.handlers:
    _log_handler = logging.StreamHandler()
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _app_logger.addHandler(_log_handler)
    _app_logger.setLevel(logging.INFO)
    _app_logger.propagate = False


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

    # start_csms() itself logs a port it cannot bind and returns None; the OSError guard keeps
    # that non-fatal here too.
    csms_server = None
    try:
        csms_server = await start_csms()
    except OSError as exc:
        logger.error(
            "OCPP CSMS could not listen on port %d, the HTTP API keeps running without it: %s",
            settings.ocpp_port, exc,
        )
    try:
        yield
    finally:
        if csms_server is not None:
            try:
                await stop_csms(csms_server)
            except Exception:
                logger.exception("Error while stopping the OCPP CSMS")


app = FastAPI(title="GreenCharge", lifespan=lifespan)
app.include_router(grid.router)
app.include_router(sites.router)
app.include_router(debug.router)
app.include_router(sessions.router)


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
