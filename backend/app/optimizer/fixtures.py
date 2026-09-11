"""Deterministic optimizer test cases (build spec, Phase 3 "Fixtures").

Each fixture returns ``(OptimizerInput, check)``, where ``check(result) -> (passed, detail)``
verifies the fixture's EXPECTED line against an ``OptimizerResult``. Acceptance test, run from
backend/:

    python -m app.optimizer.fixtures --run-all

For each fixture it prints the name, the solve status, PASS/FAIL with the check's detail, and
the time ``optimize`` took. It exits 1 unless every fixture passes. The runner also requires
``fixture_site_constrained`` to solve in under 2 seconds (the check only sees the result, so
the runner does the timing).

Where the numbers come from (none invented; only JSON files are read, never the database):
- carbon: the 96 BASE values of data/carbon_profile_in_we.json, i.e. the synthetic profile
  without its noise. Slot t starts at local 00:00 + 15*t minutes, so the horizon is one local
  day and the profile's green window (the 520 g/kWh step) is slots 44-59, 11:00-15:00.
- price: data/tariff_gerc.json ``energy_charge_by_hour[str(t // 4)]``, the hour of slot t.
- power ratings: a vehicle's ``max_ac_kw`` from data/vehicles.json, the seeded chargers' 22 kW
  rating and the seeded site's 40 kW limit. Energies, time windows, the 50 kW limit and the
  pass thresholds are the spec's fixture docstrings, as pinned by the implementation contract.
- alpha, beta, slot_hours, efficiency: the OptimizerInput defaults.
"""
import argparse
import json
import sys
import time
from collections.abc import Callable
from typing import Any

from app.config import settings
from app.optimizer.types import OptimizerInput, OptimizerResult, SessionInput

Check = Callable[[OptimizerResult], tuple[bool, str]]
Fixture = Callable[[], tuple[OptimizerInput, Check]]

CARBON_PROFILE_FILE = "carbon_profile_in_we.json"
TARIFF_FILE = "tariff_gerc.json"
VEHICLES_FILE = "vehicles.json"

_MINUTES_PER_HOUR = 60
N_SLOTS = settings.horizon_slots                              # 96
SLOTS_PER_HOUR = _MINUTES_PER_HOUR // settings.slot_minutes   # 4

# Seeded hardware (spec Phase 1 seed data; the site limit is contract decision 3).
SITE_LIMIT_KW = 40.0      # the site's max_power_kw, as in app.seed.SITE_MAX_POWER_KW
CHARGER_MAX_KW = 22.0     # CP001..CP006 max_power_kw, as in app.seed.CHARGER_MAX_POWER_KW

# Numerical slack when comparing solver output with kW / kWh limits.
TOLERANCE = 1e-6

# fixture_single_flexible
FLEX_ENERGY_KWH = 20.0              # "20 kWh needed"
FLEX_VEHICLE_MODEL = "Tata Nexon EV"  # max_kw = its max_ac_kw in data/vehicles.json
FLEX_AVAILABLE = range(36, 84)      # slots 36-83 = 09:00-21:00 local: "12 hours available"
FLEX_GREEN_WINDOW = range(44, 61)   # "slots 44-60", inclusive
FLEX_MIN_GREEN_SHARE = 0.80         # ">80% of energy"

# fixture_site_constrained
SITE_CARS = 6                       # "Six cars"
SITE_ENERGY_KWH = 30.0              # "all need 30 kWh"
SITE_AVAILABLE = range(40, 72)      # slots 40-71 = 10:00-18:00 local: "same 8 hours"
SITE_CONSTRAINED_LIMIT_KW = 50.0    # "site limit 50 kW"
SITE_MAX_SOLVE_S = 2.0              # acceptance test: "under 2 seconds"

# fixture_infeasible
INFEASIBLE_ENERGY_KWH = 60.0        # "60 kWh needed"
INFEASIBLE_MAX_KW = 7.0             # "7 kW max"
INFEASIBLE_AVAILABLE = range(72, 76)  # slots 72-75 = 18:00-19:00 local: "1 hour available"
INFEASIBLE_MIN_UNMET_KWH = 50.0     # "unmet_energy_kwh > 50"


# --------------------------------------------------------------------------- data

def _read_data(name: str) -> Any:
    with (settings.data_dir / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def _carbon_curve() -> list[float]:
    """The synthetic carbon profile's 96 base values (gCO2/kWh), without its noise."""
    values = _read_data(CARBON_PROFILE_FILE)["carbon_intensity"]
    if len(values) != N_SLOTS:
        raise ValueError(
            f"{CARBON_PROFILE_FILE}: expected {N_SLOTS} carbon_intensity values, got {len(values)}"
        )
    return [float(v) for v in values]


def _price_curve() -> list[float]:
    """The tariff's energy charge (INR/kWh) for the local hour of each slot."""
    by_hour = _read_data(TARIFF_FILE)["energy_charge_by_hour"]
    return [float(by_hour[str(t // SLOTS_PER_HOUR)]) for t in range(N_SLOTS)]


def _vehicle_max_ac_kw(model: str) -> float:
    for vehicle in _read_data(VEHICLES_FILE)["vehicles"]:
        if vehicle["model"] == model:
            return float(vehicle["max_ac_kw"])
    raise ValueError(f"{VEHICLES_FILE}: no vehicle {model!r}")


def _available(slots: range) -> list[bool]:
    return [t in slots for t in range(N_SLOTS)]


def _slot_label(slots: range) -> str:
    return f"slots {slots.start}-{slots.stop - 1}"


# --------------------------------------------------------------------------- checks

def _schedule_problems(inp: OptimizerInput, result: OptimizerResult) -> list[str]:
    """Why ``result.schedule`` cannot be checked: sessions missing or not 96 values long."""
    if not isinstance(result.schedule, dict):
        return [f"schedule is {type(result.schedule).__name__}, expected dict"]
    problems = []
    for s in inp.sessions:
        values = result.schedule.get(s.session_id)
        if values is None:
            problems.append(f"session {s.session_id} missing from schedule")
        elif len(values) != N_SLOTS:
            problems.append(
                f"session {s.session_id} has {len(values)} schedule values, expected {N_SLOTS}"
            )
    return problems


def _delivered_kwh(inp: OptimizerInput, values: list[float]) -> float:
    """Energy into the battery: sum of p * slot_hours * efficiency (the left side of C1)."""
    return sum(values) * inp.slot_hours * inp.efficiency


def _status_condition(result: OptimizerResult, expected: str) -> tuple[bool, str]:
    return result.status == expected, f"status {result.status!r} (need {expected!r})"


def _verdict(conditions: list[tuple[bool, str]]) -> tuple[bool, str]:
    """All conditions hold? Detail lists every condition, failed ones marked FAILED."""
    passed = all(ok for ok, _ in conditions)
    detail = "; ".join(text if ok else f"FAILED {text}" for ok, text in conditions)
    return passed, detail


# --------------------------------------------------------------------------- fixtures

def fixture_single_flexible() -> tuple[OptimizerInput, Check]:
    """One car, 20 kWh needed, 12 hours available, obvious green window at slots 44-60.
    EXPECTED: >80% of energy lands in slots 44-60."""
    session = SessionInput(
        session_id=1,
        energy_needed_kwh=FLEX_ENERGY_KWH,
        max_kw=_vehicle_max_ac_kw(FLEX_VEHICLE_MODEL),
        available=_available(FLEX_AVAILABLE),
    )
    inp = OptimizerInput(
        carbon=_carbon_curve(),
        price=_price_curve(),
        sessions=[session],
        site_limit_kw=SITE_LIMIT_KW,
    )

    def check(result: OptimizerResult) -> tuple[bool, str]:
        # PASS iff status optimal, energy met, and > 80 % of sum(p) in slots 44-60 inclusive.
        problems = _schedule_problems(inp, result)
        if problems:
            return False, "FAILED " + "; ".join(problems)
        p = result.schedule[session.session_id]
        total = sum(p)
        share = sum(p[t] for t in FLEX_GREEN_WINDOW) / total if total > 0 else 0.0
        delivered = _delivered_kwh(inp, p)
        energy_met = delivered >= session.energy_needed_kwh - TOLERANCE
        energy_text = f"delivered {delivered:.3f} kWh (need >= {session.energy_needed_kwh:g})"
        if not energy_met:
            energy_text += f", short by {session.energy_needed_kwh - delivered:.3g} kWh"
        return _verdict([
            _status_condition(result, "optimal"),
            (
                share > FLEX_MIN_GREEN_SHARE,
                f"{share:.1%} of energy in {_slot_label(FLEX_GREEN_WINDOW)} "
                f"(need > {FLEX_MIN_GREEN_SHARE:.0%})",
            ),
            (energy_met, energy_text),
        ])

    return inp, check


def fixture_site_constrained() -> tuple[OptimizerInput, Check]:
    """Six cars, all need 30 kWh, all available same 8 hours,
    site limit 50 kW forces them to take turns.
    EXPECTED: no slot exceeds 50 kW total; all six meet energy."""
    available = _available(SITE_AVAILABLE)
    inp = OptimizerInput(
        carbon=_carbon_curve(),
        price=_price_curve(),
        sessions=[
            SessionInput(
                session_id=i + 1,
                energy_needed_kwh=SITE_ENERGY_KWH,
                max_kw=CHARGER_MAX_KW,
                available=list(available),
            )
            for i in range(SITE_CARS)
        ],
        site_limit_kw=SITE_CONSTRAINED_LIMIT_KW,
    )

    def check(result: OptimizerResult) -> tuple[bool, str]:
        # PASS iff status optimal, max_t sum_s p <= 50 (+ tolerance) and every session delivers
        # sum(p) * dt * efficiency >= 30 (- tolerance). The runner adds the < 2 s solve time.
        problems = _schedule_problems(inp, result)
        if problems:
            return False, "FAILED " + "; ".join(problems)
        rows = [result.schedule[s.session_id] for s in inp.sessions]
        peak = max(sum(row[t] for row in rows) for t in range(N_SLOTS))
        within_limit = peak <= inp.site_limit_kw + TOLERANCE
        peak_text = f"peak site load {peak:.3f} kW (need <= {inp.site_limit_kw:g})"
        if not within_limit:
            peak_text += f", over by {peak - inp.site_limit_kw:.3g} kW"
        delivered = {s.session_id: _delivered_kwh(inp, row) for s, row in zip(inp.sessions, rows)}
        shortfall = {
            s.session_id: s.energy_needed_kwh - delivered[s.session_id] for s in inp.sessions
        }
        short = [sid for sid, kwh in shortfall.items() if kwh > TOLERANCE]
        energy_text = (
            f"lowest delivery {min(delivered.values()):.3f} kWh "
            f"(need >= {SITE_ENERGY_KWH:g} for each of {len(inp.sessions)} cars)"
        )
        if short:
            energy_text += (
                f", short: session(s) {short} by up to "
                f"{max(shortfall[sid] for sid in short):.3g} kWh"
            )
        return _verdict([
            _status_condition(result, "optimal"),
            (within_limit, peak_text),
            (not short, energy_text),
        ])

    return inp, check


def fixture_infeasible() -> tuple[OptimizerInput, Check]:
    """One car, 60 kWh needed, 1 hour available at 7 kW max.
    EXPECTED: status == 'relaxed', unmet_energy_kwh > 50."""
    session = SessionInput(
        session_id=1,
        energy_needed_kwh=INFEASIBLE_ENERGY_KWH,
        max_kw=INFEASIBLE_MAX_KW,
        available=_available(INFEASIBLE_AVAILABLE),
    )
    inp = OptimizerInput(
        carbon=_carbon_curve(),
        price=_price_curve(),
        sessions=[session],
        site_limit_kw=SITE_LIMIT_KW,
    )

    def check(result: OptimizerResult) -> tuple[bool, str]:
        # PASS iff status == "relaxed" and unmet_energy_kwh[session] > 50.
        unmet_map = result.unmet_energy_kwh if isinstance(result.unmet_energy_kwh, dict) else {}
        unmet = unmet_map.get(session.session_id)
        if unmet is None:
            unmet_condition = (
                False,
                f"no unmet_energy_kwh entry for session {session.session_id} "
                f"(need > {INFEASIBLE_MIN_UNMET_KWH:g})",
            )
        else:
            unmet_condition = (
                unmet > INFEASIBLE_MIN_UNMET_KWH,
                f"unmet {unmet:.3f} kWh (need > {INFEASIBLE_MIN_UNMET_KWH:g})",
            )
        return _verdict([_status_condition(result, "relaxed"), unmet_condition])

    return inp, check


FIXTURES: tuple[Fixture, ...] = (
    fixture_single_flexible,
    fixture_site_constrained,
    fixture_infeasible,
)

# Solve-time limits the runner enforces, by fixture name (Phase 3 acceptance test).
SOLVE_TIME_LIMIT_S: dict[str, float] = {fixture_site_constrained.__name__: SITE_MAX_SOLVE_S}


# --------------------------------------------------------------------------- runner

def run_all() -> bool:
    """Solve and check every fixture, printing one block each. True iff all pass."""
    from app.optimizer.engine import optimize  # lazy: building fixtures needs no solver

    passed_count = 0
    for fixture in FIXTURES:
        name = fixture.__name__
        inp, check = fixture()

        start = time.perf_counter()
        try:
            result = optimize(inp)
        except Exception as exc:  # report and carry on with the other fixtures
            elapsed_s = time.perf_counter() - start
            status, passed, detail = "error", False, f"optimize raised {type(exc).__name__}: {exc}"
        else:
            elapsed_s = time.perf_counter() - start
            status = result.status
            try:
                passed, detail = check(result)
            except Exception as exc:
                passed, detail = False, f"check raised {type(exc).__name__}: {exc}"

        solve_text = f"{elapsed_s * 1000:.1f} ms"
        limit_s = SOLVE_TIME_LIMIT_S.get(name)
        if limit_s is not None:
            solve_text += f" (need < {limit_s * 1000:.0f} ms)"
            if elapsed_s >= limit_s:
                passed = False
                detail += (
                    f"; FAILED solve took {elapsed_s * 1000:.1f} ms "
                    f"(need < {limit_s * 1000:.0f} ms)"
                )

        passed_count += passed
        print(name)
        print(f"  status : {status}")
        print(f"  result : {'PASS' if passed else 'FAIL'} - {detail}")
        print(f"  solve  : {solve_text}")

    print(f"{passed_count}/{len(FIXTURES)} fixtures passed")
    return passed_count == len(FIXTURES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.optimizer.fixtures",
        description="Solve the Phase 3 optimizer fixtures and check their EXPECTED outcomes.",
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="run every fixture; exit status 1 if any fails",
    )
    args = parser.parse_args(argv)
    if not args.run_all:
        parser.error("nothing to do: pass --run-all")
    return 0 if run_all() else 1


if __name__ == "__main__":
    sys.exit(main())
