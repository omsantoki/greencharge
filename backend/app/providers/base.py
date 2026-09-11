"""Grid data primitives shared by every provider.

- ``CarbonPoint`` and ``GridProvider``: the interface from the build spec, verbatim.
- ``current_time()``: the ONLY source of "now" for Phase 1 code. Phase 2 re-points it to the
  simulation clock by changing this function's body. Other modules import the name directly,
  so change the body; do not rebind the module attribute.
- ``floor_to_slot()`` and ``resample_to_slots()``: slot alignment, and THE one linear
  interpolation function used everywhere hourly data has to become 15-minute slots.
- ``cache_points()``, ``load_cached()``, ``latest_fetch_time()``: the ``grid_data`` cache.
  Every point a provider fetches is written there; rows that already exist for
  ``(zone, ts, is_forecast)`` are left untouched.

All datetimes are timezone-aware. Naive datetimes are rejected with ``ValueError``.
"""
import logging
from abc import ABC, abstractmethod
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import engine
from app.models import GridData

logger = logging.getLogger("greencharge.providers.base")

# Slot boundaries are aligned to the Unix epoch, i.e. to UTC.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class CarbonPoint:
    ts: datetime
    carbon_intensity: float     # gCO2eq/kWh
    renewable_pct: float | None = None
    fossil_pct: float | None = None


class GridProvider(ABC):
    @abstractmethod
    async def get_latest(self, zone: str) -> CarbonPoint: ...

    @abstractmethod
    async def get_forecast(self, zone: str, hours: int = 24) -> list[CarbonPoint]: ...

    @abstractmethod
    async def get_history(self, zone: str, hours: int = 24) -> list[CarbonPoint]: ...


class ProviderError(RuntimeError):
    """A grid data source failed or answered with something unusable.

    The API layer turns this into HTTP 503 carrying the message.
    """


def current_time() -> datetime:
    """Return "now" as a timezone-aware UTC datetime.

    Every Phase 1 read of the current time goes through here. Phase 2 changes this body to
    return the simulation clock.
    """
    return datetime.now(timezone.utc)


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")


def floor_to_slot(dt: datetime, slot_minutes: int | None = None) -> datetime:
    """Floor ``dt`` to the start of its slot, keeping its tzinfo.

    Slots are aligned to the Unix epoch (UTC), so the result is the same instant whatever
    timezone ``dt`` is expressed in. For 15-minute slots this is also aligned to
    Asia/Kolkata wall-clock quarters, because +05:30 is a whole number of slots.
    Default slot length: ``settings.slot_minutes``.
    """
    _require_aware(dt, "dt")
    minutes = settings.slot_minutes if slot_minutes is None else slot_minutes
    if minutes <= 0:
        raise ValueError(f"slot_minutes must be positive, got {minutes}")
    step = timedelta(minutes=minutes)
    floored = _EPOCH + ((dt - _EPOCH) // step) * step
    return floored.astimezone(dt.tzinfo)


def _lerp(a: float, b: float, frac: float) -> float:
    return a + (b - a) * frac


def _lerp_optional(a: float | None, b: float | None, frac: float) -> float | None:
    if a is None or b is None:
        return None
    return _lerp(float(a), float(b), frac)


def resample_to_slots(
    points: list[CarbonPoint],
    start: datetime,
    n_slots: int = 96,
    slot_minutes: int = 15,
) -> list[CarbonPoint]:
    """Resample ``points`` (any spacing, e.g. hourly) onto a regular slot grid.

    THE one resampling function. Returns exactly ``n_slots`` points at
    ``start + i * slot_minutes`` for i = 0 .. n_slots-1:

    - between two data points: linear interpolation in time;
    - before the first / at-or-after the last data point: clamped to that end point's values;
    - exactly on a data point: that point's values;
    - ``renewable_pct`` / ``fossil_pct``: interpolated when both neighbours are non-None,
      otherwise None.

    Input order does not matter. With several points sharing one timestamp, the last one
    in input order wins. Raises ``ValueError`` when ``points`` is empty.
    """
    if not points:
        raise ValueError("resample_to_slots needs at least one point")
    if n_slots < 0:
        raise ValueError(f"n_slots must be >= 0, got {n_slots}")
    if slot_minutes <= 0:
        raise ValueError(f"slot_minutes must be positive, got {slot_minutes}")
    _require_aware(start, "start")

    ordered: list[CarbonPoint] = []
    for p in sorted(points, key=lambda p: p.ts):  # stable: equal timestamps keep input order
        if ordered and ordered[-1].ts == p.ts:
            ordered[-1] = p  # the last one in input order wins, on both sides of the timestamp
        else:
            ordered.append(p)
    times = [p.ts for p in ordered]
    step = timedelta(minutes=slot_minutes)

    out: list[CarbonPoint] = []
    for i in range(n_slots):
        t = start + i * step
        if t >= times[-1]:
            src = ordered[-1]
        elif t < times[0]:
            src = ordered[0]
        else:
            j = bisect_right(times, t)  # times[j-1] <= t < times[j]
            a, b = ordered[j - 1], ordered[j]
            if t == a.ts:
                src = a
            else:
                frac = (t - a.ts) / (b.ts - a.ts)
                out.append(
                    CarbonPoint(
                        ts=t,
                        carbon_intensity=_lerp(
                            float(a.carbon_intensity), float(b.carbon_intensity), frac
                        ),
                        renewable_pct=_lerp_optional(a.renewable_pct, b.renewable_pct, frac),
                        fossil_pct=_lerp_optional(a.fossil_pct, b.fossil_pct, frac),
                    )
                )
                continue
        out.append(
            CarbonPoint(
                ts=t,
                carbon_intensity=float(src.carbon_intensity),
                renewable_pct=src.renewable_pct,
                fossil_pct=src.fossil_pct,
            )
        )
    return out


def cache_points(zone: str, points: list[CarbonPoint], is_forecast: bool) -> None:
    """Write ``points`` to ``grid_data``; existing ``(zone, ts, is_forecast)`` rows are kept.

    Uses ``INSERT ... ON CONFLICT DO NOTHING``. ``fetched_at`` is the REAL wall-clock UTC
    time of the write (not ``current_time()``): it drives the real-time "at most one API
    call per 15 minutes" rule. Database errors propagate to the caller.
    """
    if not points:
        return
    fetched_at = datetime.now(timezone.utc)
    rows: dict[datetime, dict] = {}
    for p in points:
        _require_aware(p.ts, "CarbonPoint.ts")
        ts = p.ts.astimezone(timezone.utc)
        # Within one batch the first point for a timestamp wins, as it would in the table.
        rows.setdefault(
            ts,
            {
                "zone": zone,
                "ts": ts,
                "carbon_intensity": float(p.carbon_intensity),
                "renewable_pct": p.renewable_pct,
                "fossil_pct": p.fossil_pct,
                "is_forecast": is_forecast,
                "fetched_at": fetched_at,
            },
        )
    stmt = pg_insert(GridData).on_conflict_do_nothing(
        index_elements=["zone", "ts", "is_forecast"]
    )
    with engine.begin() as conn:
        conn.execute(stmt, list(rows.values()))
    logger.debug(
        "cached %d %s point(s) for %s", len(rows), "forecast" if is_forecast else "actual", zone
    )


def load_cached(
    zone: str, start: datetime, end: datetime, is_forecast: bool
) -> list[CarbonPoint]:
    """Cached points with ``start <= ts < end``, ordered by ts, timestamps in UTC."""
    _require_aware(start, "start")
    _require_aware(end, "end")
    stmt = (
        select(
            GridData.ts,
            GridData.carbon_intensity,
            GridData.renewable_pct,
            GridData.fossil_pct,
        )
        .where(
            GridData.zone == zone,
            GridData.is_forecast == is_forecast,
            GridData.ts >= start,
            GridData.ts < end,
        )
        .order_by(GridData.ts)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [
        CarbonPoint(
            ts=row.ts.astimezone(timezone.utc),
            carbon_intensity=row.carbon_intensity,
            renewable_pct=row.renewable_pct,
            fossil_pct=row.fossil_pct,
        )
        for row in rows
    ]


def latest_fetch_time(zone: str, is_forecast: bool) -> datetime | None:
    """``max(fetched_at)`` for the zone and kind (UTC), or None if nothing is cached.

    Used for the rule "never call the external API for data fetched < 15 real minutes ago".
    """
    stmt = select(func.max(GridData.fetched_at)).where(
        GridData.zone == zone, GridData.is_forecast == is_forecast
    )
    with engine.connect() as conn:
        value = conn.execute(stmt).scalar()
    return None if value is None else value.astimezone(timezone.utc)
