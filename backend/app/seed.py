"""Idempotent seeding script. Run from backend/:  python -m app.seed

1. Creates any missing tables (``Base.metadata.create_all``).
2. Upserts the site "DAU Campus Charging Hub", matched by name: latitude/longitude and grid
   zone from settings, demand charge from the tariff file (data/tariff_gerc.json),
   ``max_power_kw`` = 40.0. The 40 kW limit is a user-approved deviation from the spec's
   150 kW: the vehicles' AC limits (7.0-11 kW) cap six cars at about 47 kW, so a 150 kW limit
   would never bind (recorded in README).
3. Upserts chargers CP001..CP006, matched by ``ocpp_id``: 22.0 kW each, status "Available",
   all attached to that site.

Existing rows are updated in place, so running it again still leaves 1 site and 6 chargers.
Prints a summary of what is in the database afterwards.
"""
from sqlalchemy import func, select

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Charger, Site
from app.providers.tariff import demand_charge_inr_per_kva_month

SITE_NAME = "DAU Campus Charging Hub"
SITE_MAX_POWER_KW = 40.0  # user-approved deviation from the spec's 150 kW (see docstring)
CHARGER_OCPP_IDS = ("CP001", "CP002", "CP003", "CP004", "CP005", "CP006")
CHARGER_MAX_POWER_KW = 22.0
CHARGER_STATUS = "Available"


def seed() -> None:
    # Read the tariff first so a missing or invalid tariff file fails before any DB write.
    site_values = {
        "latitude": settings.site_lat,
        "longitude": settings.site_lon,
        "grid_zone": settings.electricity_maps_zone,
        "max_power_kw": SITE_MAX_POWER_KW,
        "demand_charge_inr_per_kva": demand_charge_inr_per_kva_month(),
    }
    charger_values = {"max_power_kw": CHARGER_MAX_POWER_KW, "status": CHARGER_STATUS}

    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        site = db.scalars(select(Site).where(Site.name == SITE_NAME)).one_or_none()
        site_created = site is None
        if site is None:
            site = Site(name=SITE_NAME, **site_values)
            db.add(site)
        else:
            for field, value in site_values.items():
                setattr(site, field, value)
        db.flush()  # assigns site.id when the site is new

        chargers_created = chargers_updated = 0
        for ocpp_id in CHARGER_OCPP_IDS:
            charger = db.scalars(
                select(Charger).where(Charger.ocpp_id == ocpp_id)
            ).one_or_none()
            if charger is None:
                db.add(Charger(ocpp_id=ocpp_id, site_id=site.id, **charger_values))
                chargers_created += 1
            else:
                charger.site_id = site.id
                for field, value in charger_values.items():
                    setattr(charger, field, value)
                chargers_updated += 1

        db.commit()

        site_chargers = db.scalars(
            select(Charger).where(Charger.site_id == site.id).order_by(Charger.id)
        ).all()
        total_sites = db.scalar(select(func.count()).select_from(Site))
        total_chargers = db.scalar(select(func.count()).select_from(Charger))

    print(
        f"Site #{site.id} '{site.name}' ({'created' if site_created else 'updated'}): "
        f"zone {site.grid_zone}, lat {site.latitude}, lon {site.longitude}, "
        f"max_power_kw {site.max_power_kw}, "
        f"demand_charge_inr_per_kva {site.demand_charge_inr_per_kva}"
    )
    print(f"Chargers: {chargers_created} created, {chargers_updated} updated")
    for charger in site_chargers:
        print(
            f"  #{charger.id} {charger.ocpp_id}  {charger.max_power_kw} kW  {charger.status}"
        )
    print(f"Database now holds {total_sites} site(s) and {total_chargers} charger(s).")


if __name__ == "__main__":
    seed()
