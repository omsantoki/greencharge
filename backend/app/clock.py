"""Simulation clock: the one source of "now" for the backend from Phase 2 on.

The demo cannot run in real time, so simulated time runs ``TIME_SCALE`` times faster than real
time (default 60: one real second is one simulated minute)::

    sim_now = sim_anchor + (time.monotonic() - mono_anchor) * time_scale

The clock starts at the real UTC time at which this module is first imported. ``set_now()`` jumps
it to any instant (for example a demo scenario that starts at 18:30 IST) by re-anchoring both
terms; simulated time then keeps flowing at ``time_scale`` from there. Elapsed time is measured
with ``time.monotonic()``, so changes to the machine's wall clock do not move the simulation.

What TIME_SCALE compresses: SIMULATED time -- battery physics dt, the departure countdown, and
every timestamp the CSMS writes to the database. OCPP message cadence (the Heartbeat interval from
BootNotification, MeterValues every 10 s) stays in REAL wall-clock seconds. Use
``sim_seconds_to_real()`` to turn a simulated duration into the real seconds to wait.

``app.providers.base.current_time()`` returns ``clock.now()``. Import the singleton
(``from app.clock import clock``) and move it only with ``set_now()``; never rebind ``clock``,
because modules that already imported it would keep the old object.

This module imports only ``app.config``, so any module can import it without an import cycle.
"""
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from app.config import settings

logger = logging.getLogger("greencharge.clock")


class SimClock:
    """Thread-safe accelerated clock. Every datetime it returns is timezone-aware UTC."""

    def __init__(self, time_scale: float) -> None:
        scale = float(time_scale)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError(f"time_scale must be a finite number > 0, got {time_scale!r}")
        self._time_scale = scale
        self._lock = threading.Lock()
        self._sim_anchor = datetime.now(timezone.utc)
        self._mono_anchor = time.monotonic()

    @property
    def time_scale(self) -> float:
        """Simulated seconds that pass per real second (read-only)."""
        return self._time_scale

    def now(self) -> datetime:
        """The current simulated time, timezone-aware UTC."""
        with self._lock:
            elapsed_real = time.monotonic() - self._mono_anchor
            return self._sim_anchor + timedelta(seconds=elapsed_real * self._time_scale)

    def set_now(self, sim_dt: datetime) -> None:
        """Jump the simulated clock to ``sim_dt``; time keeps flowing at ``time_scale`` from there.

        ``sim_dt`` must be timezone-aware (any zone; it is stored as UTC). Jumping backwards is
        allowed. Raises ``ValueError`` for a naive datetime, ``TypeError`` for a non-datetime.
        """
        if not isinstance(sim_dt, datetime):
            raise TypeError(f"sim_dt must be a datetime, got {type(sim_dt).__name__}")
        if sim_dt.tzinfo is None or sim_dt.tzinfo.utcoffset(sim_dt) is None:
            raise ValueError(f"sim_dt must be timezone-aware, got naive {sim_dt!r}")
        anchor = sim_dt.astimezone(timezone.utc)
        with self._lock:
            self._sim_anchor = anchor
            self._mono_anchor = time.monotonic()
        logger.info(
            "Simulation clock set to %s (time_scale=%g)", anchor.isoformat(), self._time_scale
        )

    def sim_seconds_to_real(self, sim_seconds: float) -> float:
        """Real wall-clock seconds it takes for ``sim_seconds`` of simulated time to pass."""
        return sim_seconds / self._time_scale


# The process-wide simulation clock. It starts at the real UTC time of first import.
clock = SimClock(settings.time_scale)
