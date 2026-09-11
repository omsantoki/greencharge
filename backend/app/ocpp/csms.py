"""The CSMS: the OCPP 1.6J WebSocket server (Central System) the charge points connect to.

Deployment (the spec asks to pick one and document it): the CSMS runs IN-PROCESS with FastAPI.
The app lifespan calls ``start_csms()`` at startup and ``stop_csms(server)`` at shutdown, so the
API routers, the OCPP handlers and the in-memory ``app.ocpp.registry`` share one process and one
event loop.

Endpoint: ``ws://0.0.0.0:{OCPP_PORT}/{ocpp_id}`` (``settings.ocpp_port``, default 9000) with
the WebSocket subprotocol ``ocpp1.6``. Right after the handshake a connection is closed when:

- the client did not agree to ``ocpp1.6`` (OCPP-J 1.6: complete the handshake without a
  subprotocol, then close) -> close code 1002;
- ``ocpp_id`` is not a row in ``chargers`` -> close code 1008 (1011 if the lookup itself fails).

Otherwise the connection is served by a ``CentralSystemChargePoint`` and registered in
``registry.registry`` under its ``ocpp_id`` until it closes. Every raw frame in both directions
goes through ``LoggingConnection`` into ``registry.ocpp_log``.

websockets 13: ``websockets.serve`` is the legacy implementation. Its connection handler takes
the single ``websocket`` argument; the request target is ``websocket.path`` and the negotiated
subprotocol is ``websocket.subprotocol`` (None when nothing was agreed).
"""
import asyncio
import logging
from urllib.parse import unquote

import websockets
from sqlalchemy import select
from websockets.exceptions import ConnectionClosed
from websockets.frames import CloseCode

from app.clock import clock
from app.config import settings
from app.db import SessionLocal
from app.models import Charger
from app.ocpp import registry
from app.ocpp.handlers import CentralSystemChargePoint

logger = logging.getLogger("greencharge.ocpp.csms")

OCPP_SUBPROTOCOL = "ocpp1.6"
CSMS_HOST = "0.0.0.0"


def ocpp_id_from_path(path: str) -> str:
    """The charge point id in a request target: ``"/CP001"`` or ``"/CP001?x=1"`` -> ``"CP001"``.

    Drops any query string and the surrounding slashes, then undoes percent-encoding
    (OCPP-J 1.6 puts the charge point identity in the URL percent-encoded).
    """
    return unquote(path.partition("?")[0].strip("/"))


def _as_text(frame: str | bytes) -> str:
    """A frame as text for the log. OCPP-J uses text frames; binary ones are decoded leniently."""
    if isinstance(frame, str):
        return frame
    return bytes(frame).decode("utf-8", errors="replace")


class LoggingConnection:
    """The connection object given to the ocpp ``ChargePoint``: wraps a websocket and logs
    every raw frame it receives ("in") or sends ("out") to ``registry.ocpp_log``.

    ocpp's ``ChargePoint`` only uses ``recv()`` and ``send()``. Frames pass through unchanged.
    """

    def __init__(self, websocket) -> None:
        self.websocket = websocket
        self.ocpp_id = ocpp_id_from_path(websocket.path)

    async def recv(self) -> str | bytes:
        frame = await self.websocket.recv()
        registry.log_frame("in", self.ocpp_id, _as_text(frame))
        return frame

    async def send(self, frame: str) -> None:
        # Logged after a successful send, so the log never shows a frame that was not sent.
        await self.websocket.send(frame)
        registry.log_frame("out", self.ocpp_id, _as_text(frame))


def _charger_exists(ocpp_id: str) -> bool:
    """True if ``ocpp_id`` is a row in ``chargers``."""
    if not ocpp_id:
        return False
    with SessionLocal() as db:
        found = db.scalar(select(Charger.id).where(Charger.ocpp_id == ocpp_id))
    return found is not None


async def on_connect(websocket) -> None:
    """Serve one charge point connection for its whole lifetime (the websockets handler)."""
    ocpp_id = ocpp_id_from_path(websocket.path)
    peer = websocket.remote_address

    if websocket.subprotocol != OCPP_SUBPROTOCOL:
        logger.warning(
            "Rejected %r from %s: subprotocol %r, expected %r",
            websocket.path, peer, websocket.subprotocol, OCPP_SUBPROTOCOL,
        )
        await websocket.close(CloseCode.PROTOCOL_ERROR, f"subprotocol {OCPP_SUBPROTOCOL} required")
        return

    # The lookup is a blocking DB call; run it in a worker thread so a slow or unreachable
    # database cannot stall the event loop that also serves the API and the other chargers.
    try:
        known = await asyncio.to_thread(_charger_exists, ocpp_id)
    except Exception:
        logger.exception("Rejected %r from %s: charger lookup failed", ocpp_id, peer)
        await websocket.close(CloseCode.INTERNAL_ERROR, "charger lookup failed")
        return
    if not known:
        logger.warning("Rejected unknown charge point %r from %s", ocpp_id, peer)
        await websocket.close(CloseCode.POLICY_VIOLATION, "unknown charge point")
        return

    cp = CentralSystemChargePoint(ocpp_id, LoggingConnection(websocket))
    conn = registry.ChargePointConnection(ocpp_id=ocpp_id, cp=cp, connected_at=clock.now())
    if registry.get(ocpp_id) is not None:
        logger.warning(
            "Charge point %s connected again; the new connection replaces the old one", ocpp_id
        )
    registry.register(conn)
    logger.info("Charge point %s connected from %s", ocpp_id, peer)

    try:
        await cp.start()  # returns only by raising, normally ConnectionClosed
    except ConnectionClosed as exc:
        logger.info("Charge point %s disconnected: %s", ocpp_id, exc)
    finally:
        # If the charge point reconnected before this connection was seen to close, the new
        # connection already replaced this one in the registry: leave that entry alone.
        if registry.get(ocpp_id) is conn:
            registry.unregister(ocpp_id)


async def start_csms() -> websockets.WebSocketServer | None:
    """Start the CSMS on ``0.0.0.0:settings.ocpp_port`` (subprotocol ``ocpp1.6``).

    Returns the running server, or None when it cannot listen (for example the port is in
    use). That failure is logged as an error instead of raised, so the API keeps running
    without the OCPP layer. ``stop_csms(None)`` is a no-op.
    """
    try:
        server = await websockets.serve(
            on_connect, CSMS_HOST, settings.ocpp_port, subprotocols=[OCPP_SUBPROTOCOL]
        )
    except OSError as exc:
        logger.error(
            "OCPP CSMS could not listen on %s:%d, running without it: %s",
            CSMS_HOST, settings.ocpp_port, exc,
        )
        return None
    logger.info(
        "OCPP CSMS listening on ws://%s:%d/{ocpp_id} (subprotocol %s)",
        CSMS_HOST, settings.ocpp_port, OCPP_SUBPROTOCOL,
    )
    return server


async def stop_csms(server: websockets.WebSocketServer | None) -> None:
    """Stop listening, close every open connection (code 1001) and wait for their handlers.

    Each handler removes its charge point from the registry as its connection closes.
    """
    if server is None:
        return
    server.close()
    await server.wait_closed()
    logger.info("OCPP CSMS stopped")
