"""Electricity Maps grid provider (BUILD_SPEC Phase 1 and Appendix A).

UNVERIFIED AGAINST THE LIVE API. No Electricity Maps token was available when this module
was written, so it has never received a real response. The base URL, the auth header, the
three endpoint paths and every response field used here are copied from BUILD_SPEC
Appendix A and nothing else. Appendix A says to verify those shapes with curl before
relying on them; until someone does, treat this module as untested. A response that does
not match the documented shape raises ProviderError carrying the raw body; nothing is
guessed. The default GRID_PROVIDER=synthetic never loads this module.

Endpoints (header ``auth-token: <token>``, query ``zone=<zone>``):

    GET /carbon-intensity/latest    carbonIntensity, datetime
    GET /power-breakdown/latest     renewablePercentage, fossilFreePercentage
                                    (fossil_pct = 100 - fossilFreePercentage)
    GET /carbon-intensity/forecast  forecast[].carbonIntensity, forecast[].datetime (hourly)

Behaviour:

- Cache first. get_latest returns the cached actual point whose hour covers "now";
  get_forecast returns the cache when it covers every slot of the requested window.
- Otherwise the API may be called, but never for data fetched less than MIN_API_INTERVAL
  (15 real minutes, BUILD_SPEC Phase 1) ago. The interval is counted per zone, separately
  for "latest" (its two endpoints are one refresh) and "forecast". The last fetch time is
  the later of the newest grid_data.fetched_at and the last call made by this process: a
  re-fetch that returns only already-cached timestamps inserts nothing, so fetched_at alone
  would miss it. A failed call counts too (a timed-out request may still have reached the
  API). Real wall-clock time is used, not current_time(), because the limit protects the
  external API; fetched_at is written with the same clock.
- While a call is not allowed: latest falls back to the point this process fetched less than
  MIN_API_INTERVAL ago, forecast to whatever part of the window is cached. With nothing to
  serve, ProviderError says when the next call is allowed and repeats this process's last
  failure for that kind if it happened less than MIN_API_INTERVAL ago (e.g. the HTTP 403
  message telling the operator to set GRID_PROVIDER=synthetic).
- Every fetched point is written through cache_points. Rows that already exist for
  (zone, ts, is_forecast) are kept as they are (the spec's UNIQUE rule), so a cache hit
  returns the first value fetched for each timestamp.
- Forecast data is hourly; resample_to_slots turns it into hours * 4 slots starting at
  floor_to_slot(current_time()). Slots outside the data are clamped, and a warning is logged.
- Appendix A documents no history endpoint, so get_history only returns cached actual
  points (those stored by earlier get_latest calls) in the history window. It never calls
  the API and never interpolates across gaps, so the result can be sparse or empty.
- HTTP 401/403: the response body is logged and ProviderError tells the operator to set
  GRID_PROVIDER=synthetic (Appendix A: the forecast endpoint may not be on the plan).

grid_data has no provider column, so rows the synthetic provider wrote for the same zone
cannot be told apart from Electricity Maps rows and would be served from the cache. Clear
that zone's grid_data rows when switching GRID_PROVIDER.
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings
from app.providers.base import (
    CarbonPoint,
    GridProvider,
    ProviderError,
    cache_points,
    current_time,
    floor_to_slot,
    latest_fetch_time,
    load_cached,
    resample_to_slots,
)

logger = logging.getLogger("greencharge.providers.electricitymaps")

# BUILD_SPEC Appendix A.
AUTH_HEADER = "auth-token"
LATEST_CI_PATH = "/carbon-intensity/latest"
LATEST_BREAKDOWN_PATH = "/power-breakdown/latest"
FORECAST_CI_PATH = "/carbon-intensity/forecast"

# BUILD_SPEC Phase 1, "DO NOT in Phase 1": "Call the Electricity Maps API more than once
# per 15 minutes". Measured in real wall-clock time.
MIN_API_INTERVAL = timedelta(minutes=15)

# BUILD_SPEC Phase 1, "Resampling rule": "Electricity Maps returns hourly data."
# A point's value applies from its datetime for this long.
DATA_INTERVAL = timedelta(hours=1)

# Not given by the spec: timeout, in seconds, for each Electricity Maps HTTP request.
HTTP_TIMEOUT_S = 10.0

# fossil_pct = 100 - fossilFreePercentage (both in percent).
_PERCENT_TOTAL = 100.0


class ElectricityMapsProvider(GridProvider):
    """Carbon intensity from the Electricity Maps v3 API (see the module docstring)."""

    source = "electricitymaps"

    def __init__(self, token: str, base_url: str = "https://api.electricitymap.org/v3") -> None:
        self._token = token
        self._base_url = base_url
        # (zone, is_forecast) -> real UTC time this process last called the API for that kind.
        self._last_call: dict[tuple[str, bool], datetime] = {}
        # zone -> (latest point, real UTC time it was fetched); fallback while calls are blocked.
        self._last_latest: dict[str, tuple[CarbonPoint, datetime]] = {}
        # (zone, is_forecast) -> (message, real UTC time) of this process's last failed call;
        # repeated while calls are blocked so the operator still sees the cause.
        self._last_error: dict[tuple[str, bool], tuple[str, datetime]] = {}
        # (zone, is_forecast) -> lock, so concurrent requests cannot both call the API.
        self._locks: dict[tuple[str, bool], asyncio.Lock] = {}

    # -- GridProvider -------------------------------------------------------------------

    async def get_latest(self, zone: str) -> CarbonPoint:
        async with self._lock(zone, is_forecast=False):
            now = current_time()
            cached = load_cached(zone, now - DATA_INTERVAL, now + DATA_INTERVAL, is_forecast=False)
            covering = [p for p in cached if p.ts <= now < p.ts + DATA_INTERVAL]
            if covering:
                return covering[-1]

            retry_at = self._retry_at(zone, is_forecast=False)
            if retry_at is not None:
                recent = self._last_latest.get(zone)
                if recent is not None and _real_now() - recent[1] < MIN_API_INTERVAL:
                    return recent[0]
                raise self._blocked_error(
                    zone,
                    is_forecast=False,
                    message=f"No cached Electricity Maps value for zone {zone} covers {now.isoformat()}, "
                    f"and the latest endpoints may not be called again before "
                    f"{retry_at.isoformat()} ({_limit_text()}).",
                )

            self._start_call(zone, is_forecast=False)
            try:
                async with self._client() as client:
                    ci_data, ci_raw = await _get_json(client, LATEST_CI_PATH, zone)
                    ts = _timestamp(ci_data, "datetime", LATEST_CI_PATH, ci_raw)
                    carbon_intensity = _number(ci_data, "carbonIntensity", LATEST_CI_PATH, ci_raw)
                    pb_data, pb_raw = await _get_json(client, LATEST_BREAKDOWN_PATH, zone)
                renewable = _number(pb_data, "renewablePercentage", LATEST_BREAKDOWN_PATH, pb_raw)
                fossil_free = _number(pb_data, "fossilFreePercentage", LATEST_BREAKDOWN_PATH, pb_raw)
            except ProviderError as exc:
                self._last_error[(zone, False)] = (str(exc), _real_now())
                raise
            self._last_error.pop((zone, False), None)
            # Appendix A documents no datetime on /power-breakdown/latest, so the two
            # responses cannot be checked against each other; both are "latest".
            point = CarbonPoint(
                ts=ts,
                carbon_intensity=carbon_intensity,
                renewable_pct=renewable,
                fossil_pct=_PERCENT_TOTAL - fossil_free,
            )
            cache_points(zone, [point], is_forecast=False)
            self._last_latest[zone] = (point, _real_now())
            return point

    async def get_forecast(self, zone: str, hours: int = 24) -> list[CarbonPoint]:
        n_slots = _n_slots(hours)
        async with self._lock(zone, is_forecast=True):
            start = floor_to_slot(current_time())
            last_slot = start + (n_slots - 1) * timedelta(minutes=settings.slot_minutes)
            cached = load_cached(
                zone, start - DATA_INTERVAL, last_slot + DATA_INTERVAL, is_forecast=True
            )
            if _covers(cached, start, last_slot):
                return _resample(cached, start, n_slots)

            retry_at = self._retry_at(zone, is_forecast=True)
            if retry_at is not None:
                if cached:
                    logger.warning(
                        "Cached Electricity Maps forecast for %s spans %s..%s, not all of %s..%s; "
                        "the API may not be called again before %s, so uncovered slots are clamped",
                        zone, cached[0].ts, cached[-1].ts, start, last_slot, retry_at,
                    )
                    return _resample(cached, start, n_slots)
                raise self._blocked_error(
                    zone,
                    is_forecast=True,
                    message=f"No cached Electricity Maps forecast for zone {zone} covers "
                    f"{start.isoformat()}..{last_slot.isoformat()}, and the forecast endpoint may "
                    f"not be called again before {retry_at.isoformat()} ({_limit_text()}).",
                )

            self._start_call(zone, is_forecast=True)
            try:
                async with self._client() as client:
                    data, raw = await _get_json(client, FORECAST_CI_PATH, zone)
                points = _parse_forecast(data, raw)
            except ProviderError as exc:
                self._last_error[(zone, True)] = (str(exc), _real_now())
                raise
            self._last_error.pop((zone, True), None)
            cache_points(zone, points, is_forecast=True)
            if not _covers(points, start, last_slot):
                logger.warning(
                    "Electricity Maps forecast for %s spans %s..%s, not all of %s..%s; "
                    "uncovered slots are clamped",
                    zone, points[0].ts, points[-1].ts, start, last_slot,
                )
            return _resample(points, start, n_slots)

    async def get_history(self, zone: str, hours: int = 24) -> list[CarbonPoint]:
        # Appendix A has no history endpoint: cached actual points only, oldest first.
        # Same window as the synthetic provider: `hours` before the current slot start,
        # which is excluded.
        if hours < 1:
            raise ValueError(f"hours must be >= 1, got {hours}")
        end = floor_to_slot(current_time())
        return load_cached(zone, end - timedelta(hours=hours), end, is_forecast=False)

    # -- internals ----------------------------------------------------------------------

    def _lock(self, zone: str, is_forecast: bool) -> asyncio.Lock:
        key = (zone, is_forecast)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _retry_at(self, zone: str, is_forecast: bool) -> datetime | None:
        """When the API may next be called for this zone and kind; None if it may be now."""
        fetched = [
            t
            for t in (self._last_call.get((zone, is_forecast)), latest_fetch_time(zone, is_forecast))
            if t is not None
        ]
        if not fetched:
            return None
        retry_at = max(fetched) + MIN_API_INTERVAL
        return retry_at if _real_now() < retry_at else None

    def _start_call(self, zone: str, is_forecast: bool) -> None:
        """Check the token, then record the call before it is made."""
        if not self._token.strip():
            raise ProviderError(
                "ELECTRICITY_MAPS_TOKEN is empty. Set it in .env, or set GRID_PROVIDER=synthetic, "
                "and restart the backend."
            )
        self._last_call[(zone, is_forecast)] = _real_now()

    def _blocked_error(self, zone: str, is_forecast: bool, message: str) -> ProviderError:
        """ProviderError for a blocked call, repeating the recent failure that caused the block."""
        failure = self._last_error.get((zone, is_forecast))
        if failure is not None and _real_now() - failure[1] < MIN_API_INTERVAL:
            message = f"{message} The last call failed: {failure[0]}"
        return ProviderError(message)

    def _client(self) -> httpx.AsyncClient:
        # One short-lived client per refresh: calls are at most one per 15 minutes, and a
        # client is never shared between event loops.
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={AUTH_HEADER: self._token},
            timeout=HTTP_TIMEOUT_S,
        )


# -- module helpers ---------------------------------------------------------------------


def _real_now() -> datetime:
    """Real wall-clock UTC time, for the API rate limit only (fetched_at uses this clock)."""
    return datetime.now(timezone.utc)


def _limit_text() -> str:
    minutes = int(MIN_API_INTERVAL.total_seconds() // 60)
    return f"BUILD_SPEC Phase 1: at most one call per {minutes} minutes"


def _n_slots(hours: int) -> int:
    if hours < 1:
        raise ValueError(f"hours must be >= 1, got {hours}")
    return timedelta(hours=hours) // timedelta(minutes=settings.slot_minutes)


def _resample(points: list[CarbonPoint], start: datetime, n_slots: int) -> list[CarbonPoint]:
    return resample_to_slots(points, start, n_slots=n_slots, slot_minutes=settings.slot_minutes)


def _covers(points: list[CarbonPoint], first_slot: datetime, last_slot: datetime) -> bool:
    """True when ts-sorted ``points`` bracket [first_slot, last_slot] with no gap in the
    data longer than DATA_INTERVAL, i.e. every slot can be interpolated without clamping."""
    relevant = [
        p for p in points if first_slot - DATA_INTERVAL <= p.ts < last_slot + DATA_INTERVAL
    ]
    if not relevant or relevant[0].ts > first_slot or relevant[-1].ts < last_slot:
        return False
    return all(b.ts - a.ts <= DATA_INTERVAL for a, b in zip(relevant, relevant[1:]))


async def _get_json(client: httpx.AsyncClient, path: str, zone: str) -> tuple[dict[str, Any], str]:
    """GET ``path`` for ``zone``; return (JSON object, raw body) or raise ProviderError."""
    try:
        response = await client.get(path, params={"zone": zone})
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"Electricity Maps GET {path} failed: {type(exc).__name__}: {exc}"
        ) from exc
    raw = response.text
    status = response.status_code
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        logger.error("Electricity Maps GET %s returned HTTP %d. Response body: %s", path, status, raw)
        raise ProviderError(
            f"Electricity Maps GET {path} returned HTTP {status}: the token was rejected or this "
            f"endpoint is not on its plan (BUILD_SPEC Appendix A: the forecast endpoint may not "
            f"be). Set GRID_PROVIDER=synthetic in .env and restart the backend to use the "
            f"synthetic provider. Response body: {raw}"
        )
    if status != httpx.codes.OK:
        logger.error("Electricity Maps GET %s returned HTTP %d. Response body: %s", path, status, raw)
        raise ProviderError(f"Electricity Maps GET {path} returned HTTP {status}. Response body: {raw}")
    try:
        data = json.loads(raw)
    except ValueError:
        raise _shape_error(path, "the body is not JSON", raw) from None
    if not isinstance(data, dict):
        raise _shape_error(path, "the body is not a JSON object", raw)
    return data, raw


def _shape_error(path: str, reason: str, raw: str) -> ProviderError:
    logger.error(
        "Electricity Maps GET %s does not match BUILD_SPEC Appendix A (%s). Raw body: %s",
        path, reason, raw,
    )
    return ProviderError(
        f"Electricity Maps GET {path}: the response does not match BUILD_SPEC Appendix A "
        f"({reason}). Not guessing. Raw body: {raw}"
    )


def _number(obj: dict[str, Any], key: str, path: str, raw: str, where: str = "") -> float:
    label = f"{where}{key}"
    if key not in obj:
        raise _shape_error(path, f"field {label!r} is missing", raw)
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _shape_error(path, f"field {label!r} is {value!r}, not a number", raw)
    try:
        number = float(value)
    except OverflowError:
        raise _shape_error(path, f"field {label!r} is out of range", raw) from None
    if not math.isfinite(number):
        raise _shape_error(path, f"field {label!r} is {value!r}, not a finite number", raw)
    return number


def _timestamp(obj: dict[str, Any], key: str, path: str, raw: str, where: str = "") -> datetime:
    label = f"{where}{key}"
    if key not in obj:
        raise _shape_error(path, f"field {label!r} is missing", raw)
    value = obj[key]
    if not isinstance(value, str):
        raise _shape_error(path, f"field {label!r} is {value!r}, not an ISO-8601 string", raw)
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        raise _shape_error(path, f"field {label!r} is {value!r}, not ISO-8601", raw) from None
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise _shape_error(path, f"field {label!r} is {value!r}, which has no UTC offset", raw)
    return ts.astimezone(timezone.utc)


def _parse_forecast(data: dict[str, Any], raw: str) -> list[CarbonPoint]:
    """forecast[] -> CarbonPoints sorted by ts (carbon intensity only, per Appendix A)."""
    path = FORECAST_CI_PATH
    if "forecast" not in data:
        raise _shape_error(path, "field 'forecast' is missing", raw)
    items = data["forecast"]
    if not isinstance(items, list) or not items:
        raise _shape_error(path, "field 'forecast' is not a non-empty list", raw)
    points: list[CarbonPoint] = []
    for i, item in enumerate(items):
        where = f"forecast[{i}]."
        if not isinstance(item, dict):
            raise _shape_error(path, f"forecast[{i}] is not an object", raw)
        points.append(
            CarbonPoint(
                ts=_timestamp(item, "datetime", path, raw, where),
                carbon_intensity=_number(item, "carbonIntensity", path, raw, where),
            )
        )
    points.sort(key=lambda p: p.ts)
    for a, b in zip(points, points[1:]):
        if a.ts == b.ts:
            raise _shape_error(path, f"datetime {a.ts.isoformat()} appears twice in 'forecast'", raw)
    return points
