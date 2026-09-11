"""Vehicle charging physics for the simulated charge points.

Both functions are the build spec's "Battery physics" block, implemented exactly as written
there (same signatures, defaults and docstrings). SOC is 0.0-1.0, power in kW, time in hours.
"""


def acceptance_kw(soc: float, max_kw: float) -> float:
    """Charge rate the vehicle will accept at a given SOC.
    Constant-current below 80%, then linear taper to 20% of max at 100%."""
    if soc < 0.80:
        return max_kw
    return max_kw * (1.0 - 0.8 * (soc - 0.80) / 0.20)

def step(soc, battery_kwh, power_kw, dt_hours, efficiency=0.92):
    """Advance SOC by one timestep."""
    delivered_kwh = power_kw * dt_hours * efficiency
    return min(1.0, soc + delivered_kwh / battery_kwh)
