"""Optimizer input and output dataclasses: the build spec's Phase 3 contract, verbatim.

Every list indexed by slot has 96 entries (one per 15-minute slot of the horizon; slot 0 is
the horizon start). Power values are kW, energies are kWh.
"""
from dataclasses import dataclass


@dataclass
class SessionInput:
    session_id: int
    energy_needed_kwh: float
    max_kw: float
    available: list[bool]      # length 96
    safety_energy_kwh: float = 0.0


@dataclass
class OptimizerInput:
    carbon: list[float]        # length 96, gCO2/kWh
    price: list[float]         # length 96, INR/kWh
    sessions: list[SessionInput]
    site_limit_kw: float
    alpha: float = 1.0         # carbon weight
    beta: float = 0.001        # cost weight
    slot_hours: float = 0.25
    efficiency: float = 0.92


@dataclass
class OptimizerResult:
    status: str                          # "optimal" | "infeasible" | "relaxed"
    schedule: dict[int, list[float]]     # session_id -> 96 kW values
    total_carbon_g: float
    total_cost_inr: float
    unmet_energy_kwh: dict[int, float]   # non-zero only when relaxed
