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
- ``live_transactions``: ocpp_id -> the transaction id the charge point last reported running,
  learned from the messages that carry one (StartTransaction, MeterValues) and dropped when it
  ends (StopTransaction, or the connector reporting Available). It is what the CSMS knows about
  a transaction the DATABASE may not: a restart forgets the sessions, but the charge points keep
  their transactions (OCPP has no call that makes one forget), and the demo reset needs a
  transaction id to stop them with. Kept across a reconnect, because the transaction is.
- ``ocpp_log``: the most recent raw OCPP-J frames in both directions, stamped with the simulation
  clock, for the operator dashboard. Cleared by the demo reset (``clear_log()``), which moves that
  clock backwards.
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
live_transactions: dict[str, int] = {}
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


def note_transaction(ocpp_id: str, transaction_id: int) -> None:
    """Remember the transaction ``ocpp_id`` is running, from any message that carried its id."""
    live_transactions[ocpp_id] = int(transaction_id)


def forget_transaction(ocpp_id: str) -> None:
    """Forget ``ocpp_id``'s transaction: it has ended. Does nothing when none was known."""
    live_transactions.pop(ocpp_id, None)


def transaction_of(ocpp_id: str) -> int | None:
    """The transaction ``ocpp_id`` is running as far as the CSMS knows, or None.

    Never a guess: with no id here nothing may be sent a RemoteStopTransaction, which a real
    charge point would answer Rejected -- or, with an id that happens to exist, honour on the
    wrong transaction.
    """
    return live_transactions.get(ocpp_id)


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


def clear_log() -> None:
    """Drop every frame in ``ocpp_log``. Called by the demo reset, which moves the simulation
    clock BACKWARDS.

    Every frame is stamped with ``clock.now()``, and the reset puts the clock back to 18:30 site
    time (``scenarios.reset_start()``), which is EARLIER than the frames already logged. The
    dashboard panel orders what it is given newest-first by that stamp, so without this the
    frames from before the reset outrank everything logged after it and stay pinned to the top
    of the panel -- the log looks frozen in the future while the header clock reads 18:30, and
    it only comes right once ~50 new frames have aged the old ones out of the ring buffer (tens
    of seconds; longer still with no scenario running). Do not remove this call: as long as the
    reset moves the clock backwards, the frames from before it can only misorder the ones after.

    The log is demo state like the rest of what the reset clears, and the charge points refill it
    within a second (Heartbeats, StatusNotifications), so a panel emptied here is never empty for
    long.
    """
    ocpp_log.clear()


def recent_frames(limit: int = 50) -> list[dict]:
    """Up to ``limit`` logged frames, newest first, as copies.

    Takes a snapshot of the log first (``list()`` of a deque is a single C call), so it is safe
    to call even from a worker thread while the event loop keeps logging.
    """
    if limit <= 0:
        return []
    snapshot = list(ocpp_log)
    return [dict(item) for item in reversed(snapshot[-limit:])]
