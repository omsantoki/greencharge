"""Tariff lookups backed by ``data/tariff_gerc.json``.

The file holds PLACEHOLDER values (see its ``source`` and ``notes`` fields). It lists all
24 hours explicitly; nothing is interpolated. Hours are site-local
(``settings.site_timezone``). Energy charges are INR/kWh, the unit the optimizer consumes.
"""
import json
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config import settings

TARIFF_FILE = "tariff_gerc.json"
_HOURS_PER_DAY = 24
_HOUR_KEYS = tuple(str(h) for h in range(_HOURS_PER_DAY))


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@lru_cache(maxsize=1)
def load_tariff() -> dict:
    """The tariff JSON as a dict, read once and cached. Treat the result as read-only.

    Raises ``ValueError`` unless ``energy_charge_by_hour`` has exactly the keys "0".."23",
    each with a numeric value.
    """
    path = settings.data_dir / TARIFF_FILE
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    by_hour = data.get("energy_charge_by_hour") if isinstance(data, dict) else None
    if not isinstance(by_hour, dict):
        raise ValueError(f"{path}: energy_charge_by_hour must be an object keyed by hour")
    missing = [k for k in _HOUR_KEYS if k not in by_hour]
    unexpected = sorted(k for k in by_hour if k not in _HOUR_KEYS)
    if missing or unexpected:
        raise ValueError(
            f'{path}: energy_charge_by_hour must list hours "0".."23" explicitly '
            f"(missing {missing}, unexpected {unexpected})"
        )
    not_numeric = [k for k in _HOUR_KEYS if not _is_number(by_hour[k])]
    if not_numeric:
        raise ValueError(f"{path}: non-numeric energy charge for hour(s) {not_numeric}")
    return data


def price_at(ts: datetime) -> float:
    """Energy charge (INR/kWh) for the site-local hour containing ``ts``."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"ts must be timezone-aware, got naive {ts!r}")
    hour = ts.astimezone(ZoneInfo(settings.site_timezone)).hour
    return float(load_tariff()["energy_charge_by_hour"][str(hour)])


def price_curve(start: datetime, n_slots: int = 96, slot_minutes: int = 15) -> list[float]:
    """``price_at`` for each slot start ``start + i * slot_minutes``, i = 0 .. n_slots-1."""
    if n_slots < 0:
        raise ValueError(f"n_slots must be >= 0, got {n_slots}")
    if slot_minutes <= 0:
        raise ValueError(f"slot_minutes must be positive, got {slot_minutes}")
    step = timedelta(minutes=slot_minutes)
    return [price_at(start + i * step) for i in range(n_slots)]


def demand_charge_inr_per_kva_month() -> float:
    """``demand_charge_inr_per_kva_month`` from the tariff file."""
    value = load_tariff().get("demand_charge_inr_per_kva_month")
    if not _is_number(value):
        raise ValueError(
            f"{TARIFF_FILE}: demand_charge_inr_per_kva_month must be a number, got {value!r}"
        )
    return float(value)
