"""Grid carbon-intensity and tariff endpoints (Phase 1).

    GET /api/grid/latest              -> CarbonPointOut
    GET /api/grid/forecast?hours=24   -> list[CarbonPointOut], exactly hours * 4 points at 15-min spacing
    GET /api/grid/history?hours=168   -> list[CarbonPointOut], oldest first
    GET /api/tariff                   -> the tariff JSON

The zone is always `settings.electricity_maps_zone`. Every point carries `source` = the active
provider's `source` ("estimated" for the synthetic fallback), so synthetic values are never presented
as measured. A ProviderError becomes HTTP 503 with the provider's message.
"""
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.providers import CarbonPoint, ProviderError, get_provider
from app.providers.tariff import load_tariff
from app.schemas import CarbonPointOut

logger = logging.getLogger("greencharge.routers.grid")

router = APIRouter(prefix="/api", tags=["grid"])


def _to_out(point: CarbonPoint, source: str) -> CarbonPointOut:
    return CarbonPointOut(
        ts=point.ts,
        carbon_intensity=point.carbon_intensity,
        renewable_pct=point.renewable_pct,
        fossil_pct=point.fossil_pct,
        source=source,
    )


def _unavailable(exc: ProviderError) -> HTTPException:
    logger.warning("Grid provider error: %s", exc)
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/grid/latest", response_model=CarbonPointOut)
async def grid_latest() -> CarbonPointOut:
    try:
        provider = get_provider()
        point = await provider.get_latest(settings.electricity_maps_zone)
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    return _to_out(point, provider.source)


@router.get("/grid/forecast", response_model=list[CarbonPointOut])
async def grid_forecast(
    hours: Annotated[int, Query(ge=1, le=72)] = 24,
) -> list[CarbonPointOut]:
    try:
        provider = get_provider()
        points = await provider.get_forecast(settings.electricity_maps_zone, hours=hours)
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    # The optimizer relies on an exact slot count; refuse to serve a short or long horizon.
    expected = hours * 60 // settings.slot_minutes
    if len(points) != expected:
        logger.error("Grid provider returned %d forecast points, expected %d", len(points), expected)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Grid provider returned {len(points)} forecast points, expected {expected} "
                f"({hours} h of {settings.slot_minutes}-minute slots)"
            ),
        )
    return [_to_out(p, provider.source) for p in points]


@router.get("/grid/history", response_model=list[CarbonPointOut])
async def grid_history(
    hours: Annotated[int, Query(ge=1, le=720)] = 168,
) -> list[CarbonPointOut]:
    try:
        provider = get_provider()
        points = await provider.get_history(settings.electricity_maps_zone, hours=hours)
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    return [_to_out(p, provider.source) for p in points]


@router.get("/tariff")
def tariff() -> dict[str, Any]:
    return load_tariff()
