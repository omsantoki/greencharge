"""GreenCharge FastAPI application.

Phase 0 scaffold: only the /health endpoint. No tables, no routers yet.
"""
import logging

import redis
from fastapi import FastAPI

from app.config import settings
from app.db import db_ok

logger = logging.getLogger("greencharge.main")

app = FastAPI(title="GreenCharge")


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
