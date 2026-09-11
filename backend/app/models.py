"""SQLAlchemy 2.0 ORM models: the Phase 1 database schema, exactly as the build spec defines it.

Tables: sites, chargers, sessions, meter_values, grid_data, schedules.

Conventions:
- Every datetime column is ``DateTime(timezone=True)`` and holds timezone-aware UTC values.
- Nullability follows the spec: only the fields the spec marks ``| None`` are nullable
  (Charger.last_heartbeat, Session.ocpp_transaction_id, GridData.renewable_pct/fossil_pct).
- The spec's defaults (Site.grid_zone, Charger.status, Session.status, the Session ``= 0.0``
  accumulators) are set both on the ORM side and as database server defaults.
- Every foreign key is indexed and uses ``ondelete="CASCADE"``. The matching one-to-many
  relationships use ``cascade="all, delete"`` with ``passive_deletes=True``, so an ORM delete
  of a parent removes loaded children and leaves unloaded ones to the database cascade.
  Child rows may be created by setting the foreign-key column alone (e.g.
  ``MeterValue(session_id=...)``); attaching them through the relationship is not required.
- The charging-session model is named ``Session`` (table ``sessions``) as in the spec. Code that
  also needs SQLAlchemy's session type imports it as
  ``from sqlalchemy.orm import Session as DbSession``.

Tables are created with ``Base.metadata.create_all(engine)`` (seed script and app startup);
there are no migrations.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Site(Base):
    """A charging site with one grid connection."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    grid_zone: Mapped[str] = mapped_column(String, default="IN-WE", server_default="IN-WE")
    max_power_kw: Mapped[float] = mapped_column(Float)  # site connection limit
    demand_charge_inr_per_kva: Mapped[float] = mapped_column(Float)

    # Loaded eagerly (one extra SELECT ... IN): a site has only a handful of chargers and
    # every reader of a site needs them.
    chargers: Mapped[list["Charger"]] = relationship(
        back_populates="site",
        order_by=lambda: Charger.id,
        cascade="all, delete",
        passive_deletes=True,
        lazy="selectin",
    )


class Charger(Base):
    """A charge point (CP), identified on the OCPP side by ``ocpp_id``."""

    __tablename__ = "chargers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    ocpp_id: Mapped[str] = mapped_column(String, unique=True)  # e.g. "CP001"
    max_power_kw: Mapped[float] = mapped_column(Float)
    # Available|Preparing|Charging|Faulted|Finishing
    status: Mapped[str] = mapped_column(String, default="Available", server_default="Available")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped["Site"] = relationship(back_populates="chargers")
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="charger",
        cascade="all, delete",
        passive_deletes=True,
    )


class Session(Base):
    """One car plugged into one charger, from plug-in to unplug (table ``sessions``)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    charger_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chargers.id", ondelete="CASCADE"), index=True
    )
    ocpp_transaction_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_model: Mapped[str] = mapped_column(String)
    battery_kwh: Mapped[float] = mapped_column(Float)
    max_charge_kw: Mapped[float] = mapped_column(Float)
    soc_start: Mapped[float] = mapped_column(Float)  # 0.0-1.0
    soc_target: Mapped[float] = mapped_column(Float)
    soc_current: Mapped[float] = mapped_column(Float)
    plugged_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    energy_delivered_kwh: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0.0")
    )
    co2_actual_g: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0.0"))
    co2_baseline_g: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0.0"))
    cost_actual_inr: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0.0")
    )
    cost_baseline_inr: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0.0")
    )
    # active|completed|aborted
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")

    charger: Mapped["Charger"] = relationship(back_populates="sessions")
    meter_values: Mapped[list["MeterValue"]] = relationship(
        order_by=lambda: (MeterValue.ts, MeterValue.id),
        cascade="all, delete",
        passive_deletes=True,
    )
    schedules: Mapped[list["Schedule"]] = relationship(
        order_by=lambda: (Schedule.computed_at, Schedule.slot_start, Schedule.id),
        cascade="all, delete",
        passive_deletes=True,
    )


class MeterValue(Base):
    """A meter reading reported by a charger during a session."""

    __tablename__ = "meter_values"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    power_kw: Mapped[float] = mapped_column(Float)
    energy_kwh: Mapped[float] = mapped_column(Float)  # cumulative
    soc: Mapped[float] = mapped_column(Float)


class GridData(Base):
    """A cached grid carbon-intensity point (measured/estimated actual, or forecast)."""

    __tablename__ = "grid_data"
    __table_args__ = (
        UniqueConstraint("zone", "ts", "is_forecast", name="uq_grid_data_zone_ts_is_forecast"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    zone: Mapped[str] = mapped_column(String)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    carbon_intensity: Mapped[float] = mapped_column(Float)  # gCO2eq/kWh
    renewable_pct: Mapped[float | None] = mapped_column(Float)
    fossil_pct: Mapped[float | None] = mapped_column(Float)
    is_forecast: Mapped[bool] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Schedule(Base):
    """One planned 15-minute slot of charging power for a session."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    power_kw: Mapped[float] = mapped_column(Float)
