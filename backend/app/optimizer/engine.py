"""The charging-schedule optimizer: the build spec's Phase 3 linear program.

``optimize(inp)`` is a pure function of its input. It reads no database, network, clock or file
of its own and keeps no state between calls, so identical input gives identical output. The only
side effects are PuLP's: it writes the model to a temporary file, solves it with the CBC binary
bundled with PuLP (a subprocess), and deletes its temporary files when the solve succeeds. If CBC
fails, PuLP leaves them in the system temp directory.

The model (spec, "The model"), over sessions s and slots t = 0..95, with dt = slot_hours and
eta = efficiency:

    minimise  sum_s sum_t  p[s][t] * dt * (alpha * carbon[t] / 1000 + beta * price[t])

    (C1)  sum_t p[s][t] * dt * eta         >= energy_needed[s]     for every s
    (C2)  0 <= p[s][t] <= max_kw[s] * available[s][t]              for every s, t
    (C3)  sum_s p[s][t]                    <= site_limit_kw        for every t
    (C4)  sum_{t<4} p[s][t] * dt * eta     >= safety_energy[s]     for every s

Every variable is continuous: this is an LP, with no integer or binary variables. (C2) is built
by creating p[s][t] only where the session is available, bounded 0..max_kw. Where it is not
available, p[s][t] does not exist and the schedule holds 0.0.

Result status (spec, "Infeasibility handling"; implementation contract 5c):
- "optimal":    CBC solved the model above. unmet_energy_kwh is 0.0 for every session.
- "relaxed":    CBC did not report Optimal, so the model was solved again with (C1) made soft:
                sum_t p[s][t] * dt * eta + unmet[s] >= energy_needed[s], with unmet[s] >= 0 and
                M * sum_s unmet[s] added to the objective. unmet_energy_kwh[s] is unmet[s].
- "infeasible": the relaxed model was not solved either, for example because (C4) cannot be met.
                Every schedule value is 0.0 and unmet_energy_kwh[s] is energy_needed[s].
Solver outcomes never raise and never return None. A CBC binary that is missing, exits with an
error or writes no solution counts as "not Optimal" (PuLP raises PulpSolverError, caught in
_solve). An OSError from starting the CBC process (for example the x86_64 binary on Apple Silicon
without Rosetta) is not caught and propagates.

Malformed input is a programming error and raises ValueError: a carbon, price or available list
that is not 96 long, a number that is NaN or infinite (PuLP refuses it in the model, CBC cannot
read an infinite bound, and a NaN in an unused slot would turn the totals into NaN), a repeated
session_id, or a negative max_kw (PuLP writes a negative upper bound in a way that CBC reads as
"no lower bound", so the model would no longer be the spec's).
"""
import math

import pulp

from app.config import settings
from app.optimizer.types import OptimizerInput, OptimizerResult, SessionInput

N_SLOTS = settings.horizon_slots  # 96 slots of 15 minutes, the spec's horizon

# Spec, "Infeasibility handling": M * sum_s unmet[s] joins the objective "with a large M (use 1e6)".
UNMET_PENALTY_M = 1e6

# Spec (C4): the safety energy must arrive in slots t < 4, the first hour of the horizon.
SAFETY_WINDOW_SLOTS = 4

# Spec objective: alpha * carbon[t] / 1000 turns gCO2/kWh into kgCO2/kWh.
GRAMS_PER_KG = 1000.0

# Output hygiene (implementation contract 5c): a solver value smaller than this is reported as 0.0.
ZERO_TOLERANCE = 1e-6

STATUS_OPTIMAL = "optimal"
STATUS_RELAXED = "relaxed"
STATUS_INFEASIBLE = "infeasible"

# (session_id, slot) -> p[s][t]; only slots where the session is available have a variable.
PowerVars = dict[tuple[int, int], pulp.LpVariable]


def optimize(inp: OptimizerInput) -> OptimizerResult:
    """Plan each session's charging power (kW) for every slot of the horizon.

    Raises ValueError for malformed input. Every solver outcome is a result: "optimal",
    "relaxed" or "infeasible" (see the module docstring).
    """
    _validate(inp)
    if not inp.sessions:
        return OptimizerResult(
            status=STATUS_OPTIMAL,
            schedule={},
            total_carbon_g=0.0,
            total_cost_inr=0.0,
            unmet_energy_kwh={},
        )

    solved, power, _ = _solve(inp, relaxed=False)
    if solved:
        status = STATUS_OPTIMAL
        unmet = {s.session_id: 0.0 for s in inp.sessions}
    else:
        solved, power, unmet_vars = _solve(inp, relaxed=True)
        if not solved:
            return _infeasible_result(inp)
        status = STATUS_RELAXED
        unmet = {sid: _clean_kwh(var.varValue) for sid, var in unmet_vars.items()}

    schedule = {s.session_id: _schedule_row(s, power) for s in inp.sessions}
    return OptimizerResult(
        status=status,
        schedule=schedule,
        total_carbon_g=_grid_energy_total(inp, schedule, inp.carbon),
        total_cost_inr=_grid_energy_total(inp, schedule, inp.price),
        unmet_energy_kwh=unmet,
    )


def _validate(inp: OptimizerInput) -> None:
    """Raise ValueError if the input does not fit the model."""
    for name, values in (("carbon", inp.carbon), ("price", inp.price)):
        if len(values) != N_SLOTS:
            raise ValueError(f"{name} has {len(values)} values, expected {N_SLOTS}")
        if not all(math.isfinite(v) for v in values):
            raise ValueError(f"{name} has a value that is NaN or infinite")
    for name in ("site_limit_kw", "alpha", "beta", "slot_hours", "efficiency"):
        if not math.isfinite(getattr(inp, name)):
            raise ValueError(f"{name} is {getattr(inp, name)}, must be finite")
    seen: set[int] = set()
    for s in inp.sessions:
        if s.session_id in seen:
            raise ValueError(f"session_id {s.session_id} appears more than once")
        seen.add(s.session_id)
        if len(s.available) != N_SLOTS:
            raise ValueError(
                f"session {s.session_id}: available has {len(s.available)} values, "
                f"expected {N_SLOTS}"
            )
        for name in ("energy_needed_kwh", "max_kw", "safety_energy_kwh"):
            if not math.isfinite(getattr(s, name)):
                raise ValueError(
                    f"session {s.session_id}: {name} is {getattr(s, name)}, must be finite"
                )
        if s.max_kw < 0:
            raise ValueError(f"session {s.session_id}: max_kw is {s.max_kw}, must be >= 0")


def _solve(
    inp: OptimizerInput, relaxed: bool
) -> tuple[bool, PowerVars, dict[int, pulp.LpVariable]]:
    """Build the spec's LP, with (C1) soft if ``relaxed``, and solve it with CBC.

    Returns (solved, power, unmet). ``solved`` is True only when CBC reports Optimal. ``unmet``
    maps session_id to unmet[s] and is empty unless ``relaxed``. Variable values are meaningful
    only when ``solved`` is True.
    """
    dt, eta = inp.slot_hours, inp.efficiency
    prob = pulp.LpProblem("greencharge_relaxed" if relaxed else "greencharge", pulp.LpMinimize)

    # (C2): p[s][t] exists only where the session is available, with 0 <= p[s][t] <= max_kw.
    power: PowerVars = {
        (s.session_id, t): pulp.LpVariable(f"p_{s.session_id}_{t}", lowBound=0, upBound=s.max_kw)
        for s in inp.sessions
        for t in range(N_SLOTS)
        if s.available[t]
    }
    unmet = (
        {s.session_id: pulp.LpVariable(f"unmet_{s.session_id}", lowBound=0) for s in inp.sessions}
        if relaxed
        else {}
    )

    # Objective: sum of p[s][t] * dt * (alpha * carbon[t] / 1000 + beta * price[t]), plus
    # M * sum_s unmet[s] in the relaxed model.
    slot_weight = [
        dt * (inp.alpha * inp.carbon[t] / GRAMS_PER_KG + inp.beta * inp.price[t])
        for t in range(N_SLOTS)
    ]
    objective = pulp.lpSum(slot_weight[t] * p for (_, t), p in power.items())
    if relaxed:
        objective += UNMET_PENALTY_M * pulp.lpSum(unmet.values())
    prob += objective, "objective"

    for s in inp.sessions:
        sid = s.session_id
        own = [(t, power[sid, t]) for t in range(N_SLOTS) if (sid, t) in power]
        delivered = pulp.lpSum(dt * eta * p for _, p in own)
        if relaxed:
            delivered += unmet[sid]
        prob += delivered >= s.energy_needed_kwh, f"energy_{sid}"  # (C1)
        prob += (  # (C4)
            pulp.lpSum(dt * eta * p for t, p in own if t < SAFETY_WINDOW_SLOTS)
            >= s.safety_energy_kwh,
            f"safety_{sid}",
        )
    for t in range(N_SLOTS):  # (C3)
        prob += (
            pulp.lpSum(power[s.session_id, t] for s in inp.sessions if (s.session_id, t) in power)
            <= inp.site_limit_kw,
            f"site_{t}",
        )

    try:
        prob.solve(pulp.PULP_CBC_CMD(msg=False))
    except pulp.PulpSolverError:  # CBC not found, exited with an error or wrote no solution
        return False, power, unmet
    return prob.status == pulp.LpStatusOptimal, power, unmet


def _schedule_row(session: SessionInput, power: PowerVars) -> list[float]:
    """The session's 96 kW values: solver values where available, 0.0 elsewhere."""
    row = []
    for t in range(N_SLOTS):
        var = power.get((session.session_id, t))
        row.append(0.0 if var is None else _clean_kw(var.varValue, session.max_kw))
    return row


def _clean_kw(value: float | None, max_kw: float) -> float:
    """Clamp a solver power value to [0, max_kw]; below ZERO_TOLERANCE it becomes 0.0."""
    kw = min(max(float(value or 0.0), 0.0), float(max_kw))
    return 0.0 if abs(kw) < ZERO_TOLERANCE else kw


def _clean_kwh(value: float | None) -> float:
    """A solver unmet-energy value, never negative; below ZERO_TOLERANCE it becomes 0.0."""
    kwh = max(float(value or 0.0), 0.0)
    return 0.0 if abs(kwh) < ZERO_TOLERANCE else kwh


def _grid_energy_total(
    inp: OptimizerInput, schedule: dict[int, list[float]], per_kwh: list[float]
) -> float:
    """Sum of p[s][t] * dt * per_kwh[t]: grid-side energy (kWh) times gCO2/kWh or INR/kWh."""
    return math.fsum(
        row[t] * inp.slot_hours * per_kwh[t] for row in schedule.values() for t in range(N_SLOTS)
    )


def _infeasible_result(inp: OptimizerInput) -> OptimizerResult:
    """No usable plan: every session gets an all-zero schedule and unmet = energy_needed."""
    return OptimizerResult(
        status=STATUS_INFEASIBLE,
        schedule={s.session_id: [0.0] * N_SLOTS for s in inp.sessions},
        total_carbon_g=0.0,
        total_cost_inr=0.0,
        unmet_energy_kwh={s.session_id: float(s.energy_needed_kwh) for s in inp.sessions},
    )
