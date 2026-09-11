"""Phase 4 orchestration: the live control loop that wires the data layer, the OCPP layer and the
optimizer together.

Modules:

- ``loop``: the tick (model predictive control). Every 5 simulated minutes, and immediately on
  plug-in, stop, fault, weight change and override, it re-plans every active session with
  ``app.optimizer.optimize()``, persists the full schedule, and sends SetChargingProfile for
  slot 0 only.
- ``baseline``: the shadow "charge naively at full acceptance rate from plug-in" simulation behind
  every savings number (``simulate_baseline``), and the site load curve that compares it with the
  optimized load (``site_load_curve``). It never sends OCPP commands.
- ``accounting``: CO2 and cost attribution per meter-value interval from the ACTUAL carbon
  intensity (never the forecast), and the impact summary.

This file imports nothing on purpose: ``app.ocpp.handlers`` imports ``accounting`` and ``loop``
imports the OCPP layer, so importing the submodules here would create an import cycle. Import
them directly, e.g. ``from app.orchestrator import accounting``.
"""
