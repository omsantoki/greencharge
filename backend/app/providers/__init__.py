"""Grid data providers.

``get_provider()`` returns the process-wide provider chosen by ``settings.grid_provider``:
``"synthetic"`` (estimated daily profile, the default) or ``"electricitymaps"`` (live API).
Only the chosen implementation is imported.
"""
import threading

from app.config import settings
from app.providers.base import CarbonPoint, GridProvider, ProviderError, resample_to_slots

__all__ = ["CarbonPoint", "GridProvider", "ProviderError", "get_provider", "resample_to_slots"]

_provider: GridProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> GridProvider:
    """The module-level provider singleton, created on first use."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                name = settings.grid_provider
                if name == "synthetic":
                    from app.providers.synthetic import SyntheticGridProvider

                    _provider = SyntheticGridProvider()
                elif name == "electricitymaps":
                    from app.providers.electricitymaps import ElectricityMapsProvider

                    _provider = ElectricityMapsProvider(token=settings.electricity_maps_token)
                else:
                    raise ValueError(
                        f"Unknown GRID_PROVIDER {name!r}: expected 'synthetic' or 'electricitymaps'"
                    )
    return _provider
