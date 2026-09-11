"""Synthetic grid provider: the demo safety net.

Replays ``data/carbon_profile_in_we.json``, a daily carbon-intensity curve (one value per slot)
of ESTIMATES. The file is marked ``"source": "estimated"`` and that marker is carried on
``self.source`` so the API labels every point with it: these numbers must never be presented
as measured.

Value for a timestamp ``ts`` (every number comes from the JSON file):

    slot = slot of day of ``ts`` in site-local time (settings.site_timezone)
    u    = random.Random(f"{seed}:{zone}:{slot}").uniform(-1, 1)
    CI   = carbon_intensity[slot] * (1 + amplitude_pct / 100 * u)

The noise is keyed by slot of day, so every day and every run produce identical values
(demo reproducibility). There is no separate "actual" series: forecast, latest and history
all come from the same function, ``ci_at``.

Windows, with now = floor_to_slot(current_time()) and n = hours * 60 / slot_minutes
(= hours * 4 for 15-minute slots):

    get_latest   -> the point at now
    get_forecast -> n points from now INCLUSIVE, one slot apart
    get_history  -> n points ending at now EXCLUSIVE (completed past slots), oldest first

Every returned point is written through ``cache_points`` (forecast rows with
is_forecast=True, latest and history rows with is_forecast=False).
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.providers.base import (
    CarbonPoint,
    GridProvider,
    cache_points,
    current_time,
    floor_to_slot,
)

PROFILE_FILE = "carbon_profile_in_we.json"
_MINUTES_PER_HOUR = 60
_MINUTES_PER_DAY = 24 * _MINUTES_PER_HOUR


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SyntheticGridProvider(GridProvider):
    """Deterministic replay of the estimated daily carbon-intensity profile."""

    def __init__(self, profile_path: Path | None = None) -> None:
        path = Path(profile_path) if profile_path is not None else settings.data_dir / PROFILE_FILE
        with path.open(encoding="utf-8") as fh:
            profile = json.load(fh)

        source = profile.get("source")
        if source != "estimated":
            raise ValueError(
                f'{path}: the synthetic profile must be marked "source": "estimated", '
                f"found {source!r}"
            )
        self.source: str = source

        slot_minutes = profile.get("slot_minutes")
        if slot_minutes != settings.slot_minutes:
            raise ValueError(
                f"{path}: slot_minutes {slot_minutes!r} does not match "
                f"settings.slot_minutes {settings.slot_minutes}"
            )
        self.slot_minutes: int = settings.slot_minutes
        slots_per_day, remainder = divmod(_MINUTES_PER_DAY, self.slot_minutes)
        if remainder:
            raise ValueError(f"{path}: slot_minutes {self.slot_minutes} does not divide a day")

        profile_tz = profile.get("timezone", settings.site_timezone)
        if profile_tz != settings.site_timezone:
            raise ValueError(
                f"{path}: profile timezone {profile_tz!r} does not match "
                f"settings.site_timezone {settings.site_timezone!r}"
            )
        self._tz = ZoneInfo(settings.site_timezone)

        base = profile.get("carbon_intensity")
        if not isinstance(base, list) or len(base) != slots_per_day or not all(
            _is_number(v) for v in base
        ):
            raise ValueError(f"{path}: carbon_intensity must be a list of {slots_per_day} numbers")
        self._base: list[float] = [float(v) for v in base]

        renewable = profile.get("renewable_pct")
        if renewable is not None and (
            not isinstance(renewable, list)
            or len(renewable) != slots_per_day
            or not all(v is None or _is_number(v) for v in renewable)
        ):
            raise ValueError(
                f"{path}: renewable_pct must be null or a list of {slots_per_day} numbers/nulls"
            )
        self._renewable: list[float | None] | None = (
            None if renewable is None else [None if v is None else float(v) for v in renewable]
        )

        noise = profile.get("noise")
        if (
            not isinstance(noise, dict)
            or "seed" not in noise
            or not _is_number(noise.get("amplitude_pct"))
        ):
            raise ValueError(f"{path}: noise must be an object with seed and amplitude_pct")
        self._seed = noise["seed"]
        self._amp: float = float(noise["amplitude_pct"]) / 100

        self._step = timedelta(minutes=self.slot_minutes)
        self._noise_cache: dict[tuple[str, int], float] = {}

    def _slot_of_day(self, ts: datetime) -> int:
        if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
            raise ValueError(f"ts must be timezone-aware, got naive {ts!r}")
        local = ts.astimezone(self._tz)
        return (local.hour * _MINUTES_PER_HOUR + local.minute) // self.slot_minutes

    def _noise(self, zone: str, slot: int) -> float:
        key = (zone, slot)
        u = self._noise_cache.get(key)
        if u is None:
            u = random.Random(f"{self._seed}:{zone}:{slot}").uniform(-1, 1)
            self._noise_cache[key] = u
        return u

    def ci_at(self, ts: datetime, zone: str) -> float:
        """Deterministic carbon intensity (gCO2eq/kWh) for the slot containing ``ts``."""
        slot = self._slot_of_day(ts)
        return self._base[slot] * (1 + self._amp * self._noise(zone, slot))

    def _point(self, ts: datetime, zone: str) -> CarbonPoint:
        slot = self._slot_of_day(ts)
        return CarbonPoint(
            ts=ts,
            carbon_intensity=self.ci_at(ts, zone),
            renewable_pct=None if self._renewable is None else self._renewable[slot],
            fossil_pct=None,
        )

    def _n_slots(self, hours: int) -> int:
        if hours < 1:
            raise ValueError(f"hours must be >= 1, got {hours}")
        return hours * _MINUTES_PER_HOUR // self.slot_minutes

    async def get_latest(self, zone: str) -> CarbonPoint:
        now_slot = floor_to_slot(current_time(), self.slot_minutes)
        point = self._point(now_slot, zone)
        cache_points(zone, [point], is_forecast=False)
        return point

    async def get_forecast(self, zone: str, hours: int = 24) -> list[CarbonPoint]:
        start = floor_to_slot(current_time(), self.slot_minutes)
        points = [self._point(start + i * self._step, zone) for i in range(self._n_slots(hours))]
        cache_points(zone, points, is_forecast=True)
        return points

    async def get_history(self, zone: str, hours: int = 24) -> list[CarbonPoint]:
        end = floor_to_slot(current_time(), self.slot_minutes)
        n = self._n_slots(hours)
        points = [self._point(end - (n - i) * self._step, zone) for i in range(n)]
        cache_points(zone, points, is_forecast=False)
        return points
