"""In-memory state of the OCPP layer, shared by the CSMS, the message handlers and the API.

The CSMS runs in the FastAPI process, so this state lives there and is used from its asyncio
event loop. None of it is persisted: a restart forgets it, and charge points re-register when
they reconnect.

- ``registry``: the connected charge points by ``ocpp_id``. It is the handle for outbound calls
  (SetChargingProfile, RemoteStopTransaction, DataTransfer).
- ``pending_plugins``: plug-in parameters from POST /api/debug/plug-in that are waiting for the
  charge point's StartTransaction, which creates the Session from them.
- ``session_waiters``: futures resolved with the new session id when that StartTransaction
  creates the Session.
- ``manual_limits_w``: session_id -> W limits set by an operator (debug set-limit, override).
  The Phase 4 orchestrator must respect them.
- ``ocpp_log``: the most recent raw OCPP-J frames in both directions, stamped with the simulation
  clock, for the operator dashboard.
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.clock import clock

# Number of raw frames kept in ``ocpp_log``; older frames are dropped.
OCPP_LOG_MAXLEN = 200

_DIRECTIONS = ("in", "out")


@dataclass
class ChargePointConnection:
    """One live OCPP connection. ``cp`` is the ``CentralSystemChargePoint`` serving it."""

    ocpp_id: str
    cp: Any
    connected_at: datetime


registry: dict[str, ChargePointConnection] = {}
pending_plugins: dict[str, dict] = {}
session_waiters: dict[str, asyncio.Future] = {}
manual_limits_w: dict[int, float] = {}
ocpp_log: deque[dict] = deque(maxlen=OCPP_LOG_MAXLEN)


def register(conn: ChargePointConnection) -> None:
    """Make ``conn`` the connection for its ``ocpp_id``, replacing any earlier one."""
    registry[conn.ocpp_id] = conn


def unregister(ocpp_id: str) -> None:
    """Forget the connection for ``ocpp_id``. Does nothing if there is none."""
    registry.pop(ocpp_id, None)


def get(ocpp_id: str) -> ChargePointConnection | None:
    """The live connection for ``ocpp_id``, or None when that charge point is not connected."""
    return registry.get(ocpp_id)


def log_frame(direction: str, ocpp_id: str, frame: str) -> None:
    """Append one raw OCPP-J frame to ``ocpp_log``, stamped with the simulation clock.

    ``direction`` is "in" (charge point -> CSMS) or "out" (CSMS -> charge point).
    """
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    ocpp_log.append(
        {
            "ts": clock.now().isoformat(),
            "direction": direction,
            "ocpp_id": ocpp_id,
            "frame": frame,
        }
    )


def recent_frames(limit: int = 50) -> list[dict]:
    """Up to ``limit`` logged frames, newest first, as copies.

    Takes a snapshot of the log first (``list()`` of a deque is a single C call), so it is safe
    to call even from a worker thread while the event loop keeps logging.
    """
    if limit <= 0:
        return []
    snapshot = list(ocpp_log)
    return [dict(item) for item in reversed(snapshot[-limit:])]
