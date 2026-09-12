# GreenCharge

GreenCharge is an EV Charging Network Renewable-Optimization Platform, built for HackOut'26 at
Dhirubhai Ambani University. It is built one phase at a time against the project's build
specification (`BUILD_SPEC.md`, the single source of truth, kept outside this directory); a phase
starts only after the previous phase's acceptance test passes. So far it has Phase 0 (a running
skeleton), Phase 1 (the data layer: database schema, seed data, and grid carbon-intensity, tariff
and site data over HTTP), Phase 2 (the OCPP layer: an OCPP 1.6J server inside the API process and
six simulated chargers that obey remote power limits), Phase 3 (the optimizer: a linear program
that plans each car's charging power for the next 24 hours), Phase 4 (orchestration: a control
loop that runs the optimizer on the live sessions, pushes the result to the chargers as OCPP
charging profiles, and accounts for the CO₂ and money saved against a naive-charging baseline),
Phase 5 (the operator dashboard: the single screen the demo runs on, described under
[Operator dashboard](#operator-dashboard-phase-5)), Phase 6 (the driver PWA: the phone view at
`/driver`, under [Driver app](#driver-app-phase-6)) and Phase 7 (the LLM layer: three narrow
capabilities — reading a driver's own sentence into the plug-in form, narrating a plan already on
screen, and an operator copilot over three fixed tools — none of which ever produces a number the
UI shows, under [LLM layer](#llm-layer-phase-7)).

## Prerequisites

- **Docker Desktop** (provides `docker compose` v2). Docker runs only Postgres and Redis; the backend
  and frontend run on the host. `backend/Dockerfile` is in the spec's file manifest, but
  `docker-compose.yml` does not use it.
- **Python 3.11 via pyenv** — this project uses 3.11.9 (`pyenv install 3.11.9`).
- **Node.js 18+** with npm.

## One-time setup

Run from the repository root (`greencharge/`). If your path contains an apostrophe (e.g. `HackOut'26`),
wrap it in double quotes in the shell.

```bash
"$(pyenv root)/versions/3.11.9/bin/python3.11" -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/pip install -r simulator/requirements.txt   # same pins as the backend's; installs nothing new
(cd frontend && npm install)
cp .env.example .env
```

API keys go in `.env` only, never in code or git (`.env` is gitignored). No keys are needed to run:
`GRID_PROVIDER` defaults to `synthetic`, and with `LLM_API_KEY` empty the three Phase 7 endpoints
answer 503 while every other feature behaves exactly as it does with a key
(see [LLM layer](#llm-layer-phase-7)).

## Run

1. Postgres (host port 5435) and Redis (port 6379), from the repository root:

   ```bash
   docker compose up -d
   ```

   Add `--wait` to block until both healthchecks pass.

2. Backend (port 8000), from `backend/` using the venv. Seed the database, then start the API:

   ```bash
   cd backend
   source ../.venv/bin/activate
   python -m app.seed
   uvicorn app.main:app --reload --port 8000
   ```

   `python -m app.seed` creates the tables and upserts the site "DAU Campus Charging Hub" and chargers
   `CP001`–`CP006`. It is idempotent: running it again updates those rows in place, so the database
   still holds 1 site and 6 chargers. A charger's `status` and `last_heartbeat` are live OCPP state:
   a new charger row starts as `Available`, and a re-run leaves both as they are. The API creates
   missing tables at startup too, but only the seed script inserts the site and chargers.

   The same process also runs the OCPP server on port 9000 (see
   [Simulator & OCPP](#simulator--ocpp-phase-2)).

3. Frontend dev server (port 5173), from `frontend/` in another terminal:

   ```bash
   cd frontend
   npm run dev
   ```

   The dev server proxies `/api` and `/health` to `http://localhost:8000`. To point it at a backend
   on another port, set `VITE_API_TARGET` (see
   [Operator dashboard](#operator-dashboard-phase-5)):

   ```bash
   VITE_API_TARGET=http://localhost:18907 npm run dev -- --port 5187
   ```

4. Simulated chargers, from the repository root in another terminal, using the venv:

   ```bash
   source .venv/bin/activate
   python simulator/run.py --chargers 6
   ```

Check it:

```bash
curl -s localhost:8000/health
# {"status":"ok","db":true,"redis":true}
```

`status` is always `"ok"` while the API is up; `db` and `redis` report whether each service is
reachable. Open http://localhost:5173 — the page reads "GreenCharge".

Stop the services with `docker compose down`. Postgres data is kept in the named volume `pgdata`;
`docker compose down -v` deletes it.

## API (Phase 1)

- `GET /api/grid/latest`: the current carbon-intensity point.
- `GET /api/grid/forecast?hours=24`: exactly `hours × 4` points, 15 minutes apart, starting at the
  current slot (`hours` 1–72; the default gives 96).
- `GET /api/grid/history?hours=168`: points for the `hours` before the current slot, oldest first
  (`hours` 1–720).
- `GET /api/tariff`: the tariff JSON from `backend/app/data/tariff_gerc.json`.
- `GET /api/sites`: the sites, each with its chargers nested (ordered by charger id).

Each grid point is `{ts, carbon_intensity, renewable_pct, fossil_pct, source}`, with `ts` in UTC
(ISO-8601) and `carbon_intensity` in gCO₂eq/kWh. With the default `GRID_PROVIDER=synthetic`, grid
values are synthetic **estimates**, not measurements, and every point is marked
`"source": "estimated"`. `renewable_pct` and `fossil_pct` are `null`, because the estimated profile
has no values for them. Grid points are cached in the `grid_data` table as they are generated or
fetched. Out-of-range `hours` returns 422, and a grid provider failure returns 503 with the
provider's message.

## Simulator & OCPP (Phase 2)

**The CSMS runs in-process with the API.** The spec says to run the OCPP server "in the same process
as FastAPI via a startup task, OR as a separate process — pick one and document it". GreenCharge
runs it in the same process: the FastAPI lifespan starts an OCPP 1.6J WebSocket server on
`0.0.0.0:OCPP_PORT` (default 9000) and stops it at shutdown, so the API, the OCPP handlers and the
in-memory registry of connected chargers share one process and one event loop. Charge points connect
to `ws://localhost:9000/{ocpp_id}` (for example `/CP001`) with the WebSocket subprotocol `ocpp1.6`.
A connection is closed if the client does not agree to `ocpp1.6` (close code 1002) or if `ocpp_id`
is not a charger in the database (1008). If port 9000 is taken, the error is logged and the HTTP API
keeps running without OCPP.

What the CSMS does with each message from a charge point:

| Message | Effect |
|---|---|
| `BootNotification` | `Accepted`, heartbeat interval 30 s, the current (simulated) time |
| `Heartbeat` | Sets the charger's `last_heartbeat`; replies with the current time |
| `StatusNotification` | Sets the charger's `status` |
| `StartTransaction` | Creates the session from the pending plug-in (below); transaction id = session id |
| `MeterValues` | Writes one `meter_values` row (power, energy, SoC) and updates the session's `energy_delivered_kwh` and `soc_current` |
| `StopTransaction` | Marks the session `completed`, with `energy_delivered_kwh` = meterStop / 1000 |

Every timestamp stored in the database is the simulation clock at the moment the message arrived;
timestamps sent by charge points are logged, not stored. Meter values reach the database only
through `MeterValues` messages. In the other direction the CSMS sends `SetChargingProfile` (the
spec's payload: a `TxProfile` with one period, `limit` in whole watts, duration 900 s),
`RemoteStopTransaction`, and the plug-in `DataTransfer`.

**The simulator** (`simulator/run.py`) starts CP001…CP00N in one process. Each one connects, sends
BootNotification, a Heartbeat right away and then every 30 s, and reports connector 1 as
`Available`. If the CSMS is down or the connection drops, it retries every 2 s, and a transaction in
progress continues on the new connection. Options: `--chargers N` (default 6), `--url` (default
`ws://localhost:$OCPP_PORT`) and `--max-kw` (the charger rating, which is also the power limit until
a charging profile arrives; default 22.0). It reads `TIME_SCALE` and `OCPP_PORT` from the environment
or from `.env` in the repository root, and does not import the backend.

While a car charges, the battery advances every real second with the spec's `step()` at
`min(limit, acceptance_kw(soc, max_kw))`. The limit is the charger rating until a
`SetChargingProfile` sets a new one. Every 10 real seconds the charger sends MeterValues with
`Power.Active.Import` (W), `Energy.Active.Import.Register` (Wh, restarting at 0 for every
transaction) and `SoC` (%). It stops when the SoC reaches the target (reason `Local`), when the
departure time is reached (`EVDisconnected`) or on `RemoteStopTransaction` (`Remote`). It then sends
a last MeterValues (context `Transaction.End`, power 0), StopTransaction, and StatusNotification
`Finishing`, then `Available`.

**TIME_SCALE** (default 60, in `.env.example`) is simulated seconds per real second: at 60, one real
second is one simulated minute. It compresses simulated time: the battery physics
(`dt_hours = real_elapsed × TIME_SCALE / 3600`), the departure countdown, the API's simulation
clock (`GET /api/clock`) and every timestamp written to the database. The OCPP message cadence
stays in real seconds: Heartbeat every 30 s, MeterValues every 10 s, a physics step every 1 s and a
reconnect attempt every 2 s. At 60, consecutive meter readings are therefore 10 simulated minutes
apart, and a car plugged in with `"hours_until_departure": 10` leaves after 10 real minutes. The
API and the simulator read `TIME_SCALE` separately (both from the root `.env`), so they must use
the same value.

**Plug-in relay.** OCPP 1.6 has no message for "a car was plugged in", so `POST /api/debug/plug-in`
stores the vehicle parameters in the CSMS and sends the charger an OCPP `DataTransfer` with vendorId
`GreenCharge`, messageId `SimPlugIn` and `data` = a JSON string of `vehicle_model`, `battery_kwh`,
`max_kw`, `soc_start`, `soc_target` and `hours_until_departure`. `max_kw` is the lower of the
vehicle's AC limit (from `vehicles.json`) and the charger rating. The simulated charger answers
`Accepted` (`Rejected` while a car is plugged in) and then runs StatusNotification(`Preparing`) →
StartTransaction → StatusNotification(`Charging`). The CSMS creates the session from the stored
parameters when StartTransaction arrives, and the endpoint returns once the session exists.

Endpoints:

- `POST /api/debug/plug-in` with `{"charger_id", "vehicle_model", "soc_start", "soc_target",
  "hours_until_departure"}` (SoC 0–1 with target above start; hours in simulated time) →
  `{"session_id", "transaction_id", "charger_id", "ocpp_id"}`. 404 for an unknown charger or
  vehicle; 409 if the charger is not connected, already has an active session or a plug-in in
  progress, or rejects the plug-in; 504 if no StartTransaction arrives within 15 s.
- `POST /api/debug/set-limit` with `{"charger_id", "limit_w"}` → waits up to 10 s for an active
  session on that charger, records the limit as that session's manual limit, and sends
  `SetChargingProfile` → `{"status", "session_id", "limit_w"}`. 409 if the charger is not connected,
  has no active session or rejects the profile; 504 if the charger does not answer.
- `GET /api/sessions/{id}/meter-values` → NDJSON (`application/x-ndjson`): one
  `{"ts", "power_kw", "energy_kwh", "soc"}` object per line, oldest first, so `| tail -1` is the
  newest reading. 404 for an unknown session.
- `GET /api/ocpp/log?limit=50` → the most recent raw OCPP-J frames (the last 200 are kept), newest
  first: `{"ts", "direction": "in"|"out", "ocpp_id", "frame"}`.
- `GET /api/clock` → `{"now", "time_scale"}`.

Both POST endpoints read the body as JSON whatever the Content-Type header says, so
`curl -X POST ... -d '{...}'` works without `-H`. An invalid body returns 422, and an OCPP error or
unexpected answer from the charger returns 502. The spec's Phase 2 check, with the API and the
simulator running:

```bash
curl -X POST localhost:8000/api/debug/plug-in -d '{"charger_id":1,"vehicle_model":"Tata Nexon EV","soc_start":0.3,"soc_target":0.8,"hours_until_departure":10}'
curl -X POST localhost:8000/api/debug/set-limit -d '{"charger_id":1,"limit_w":5000}'
sleep 15
curl -s localhost:8000/api/sessions/1/meter-values | tail -1   # power_kw 5.0 instead of 7.2
```

Session ids come from a database sequence, so that plug-in is session 1 only on a database with no
sessions yet (see the reset under "Decisions & deviations").

## Optimizer (Phase 3)

`optimize(OptimizerInput) -> OptimizerResult` in `backend/app/optimizer/engine.py` plans each car's
charging power (kW) for 96 slots of 15 minutes. It is a pure function (no database, network, clock
or file access of its own) that builds a linear program with PuLP and solves it with the CBC solver
bundled with PuLP. Every variable is continuous; there are no integer or binary variables.

- **Objective:** minimise `Σ_cars Σ_slots p · Δt · (α · carbon / 1000 + β · price)`, i.e. kg of CO₂
  plus a small weight on cost in INR (defaults α = 1.0, β = 0.001, Δt = 0.25 h).
- **Constraints:** (C1) each car gets its energy, `Σ p · Δt · η ≥ energy needed` with η = 0.92;
  (C2) `0 ≤ p ≤ max kW`, and 0 in slots where the car is not available; (C3) the cars' total stays
  within the site limit in every slot; (C4) a car's optional safety energy arrives in slots 0–3.
- **Relaxed path:** if CBC reports anything other than Optimal, the model is solved again with (C1)
  made soft: a car may fall short by `unmet` kWh, and each unmet kWh costs M = 10⁶ in the objective.
  The result has `status="relaxed"`, the best schedule possible and `unmet_energy_kwh` per car, which
  is what lets the UI say "I can only get you to X% by that time" instead of showing an error. If
  even that model has no solution (for example safety energy that cannot arrive in slots 0–3), the
  status is `"infeasible"`: every schedule is all zeros and each car's unmet energy is its whole
  energy need.
- Malformed input raises `ValueError`: a carbon, price or availability list that is not 96 long, a
  NaN or infinite number, a repeated session id, or a negative max kW.

The spec's three fixtures are the acceptance test. Run them from `backend/` with the venv. No Docker
service or server is needed, because the fixtures read only the JSON files in `backend/app/data/`:

```bash
cd backend
source ../.venv/bin/activate
python -m app.optimizer.fixtures --run-all
```

For each fixture it prints the solve status, PASS or FAIL with the measured values, and the solve
time. It exits 1 if any fixture fails, including when `fixture_site_constrained` takes 2 s or longer.

**Apple Silicon:** PuLP's bundled CBC binary for macOS is x86_64 only, so on Apple Silicon it runs
under Rosetta 2 (`softwareupdate --install-rosetta` if it is not installed). Without Rosetta, CBC
cannot start and `optimize` fails with an `OSError`. The first CBC run after installing the venv
takes about a second longer while macOS checks and translates the binary; later solves take tens
of milliseconds.

## Orchestration (Phase 4)

The orchestrator (`backend/app/orchestrator/`) turns the optimizer into a live control loop: it
plans every active session, executes the plan through OCPP, and keeps the books on what that
saved. It starts and stops with the API process, alongside the CSMS.

**The tick — model predictive control.** A tick runs every 5 **simulated** minutes
(`settings.tick_minutes`), which is `5 × 60 / TIME_SCALE` real seconds — 5 real seconds at the
default `TIME_SCALE=60` — driven by an APScheduler `AsyncIOScheduler`. Each tick:

1. loads every `active` session with its charger and site;
2. computes `energy_needed = max(0, (soc_target − soc_current) × battery_kwh)` and the power
   ceiling `acceptance_kw(soc_current, max_charge_kw)` — what the car will take right now;
3. builds each session's `available` mask over the 96-slot horizon that starts at the current
   15-minute slot: the current slot counts while `now < deadline`, a later slot only if it ends by
   the deadline, and a charger reporting `Faulted` is unavailable in every slot;
4. fetches the carbon forecast for the site's grid zone and the tariff, resampled to the 96 slots;
5. calls `optimize()` in a worker thread, once per site;
6. writes all 96 planned slots per session to the `schedules` table — a new version every tick,
   older versions kept, because the history is a demo asset;
7. sends each charge point `SetChargingProfile` with **slot 0 only**, as a whole number of watts;
8. logs one line: reason, sessions, solve status, solve time, planned slot-0 power, profiles
   accepted.

**Only slot 0 is ever executed.** The other 95 slots are a plan that the next tick revises with
newer SoC, newer forecast and newer cars. Ticks never overlap (one `asyncio.Lock`), and besides
the timer a tick is requested immediately after a StartTransaction (once its baseline is stored),
after a StopTransaction, on a `Faulted` status, when a charge point turns out to have lost a
transaction, on an override, and on a weights change (that one is awaited, so the caller sees the
new plan). A failed tick is logged and the chargers simply keep their last limits.

**Manual limits.** A session with a manual limit — `POST /api/sessions/{id}/override` or the
Phase 2 `POST /api/debug/set-limit` — is taken out of the linear program. What it actually draws
(the lower of its limit and its acceptance now) is subtracted from the site limit the LP has to
share out, its own limit is re-sent every tick, and a "max now" plan is stored for it so the UI can
draw it like any other session. Its limit is dropped when the session stops.

**Baseline shadow simulation** (`orchestrator/baseline.py`). When a session starts, the same
battery physics the simulator uses answer the counterfactual: *what if this car had charged at its
full acceptance rate from the moment it was plugged in, with no site limit and no coordination?*
The resulting power profile is priced with the carbon forecast and the tariff and stored once as
`co2_baseline_g` and `cost_baseline_inr`. The baseline never sends an OCPP message; it is a
simulation, not a second controller. Every savings number in the product comes from it.

**Honest accounting** (`orchestrator/accounting.py`). Actual CO₂ and cost are attributed per meter
interval, as the energy arrives: for each `MeterValues` reading (and for the final `meterStop` of a
`StopTransaction`), `co2_actual_g += Δenergy × CI` and `cost_actual_inr += Δenergy × price`, where
`CI` is the **actual** carbon intensity at that moment in the charger's site grid zone — never the
forecast — and `price` is the tariff at that moment. A negative delta (a meter register reset) is
ignored, and if the carbon intensity or the price cannot be obtained the reading is still stored
and the unattributed kWh are logged rather than guessed. (With the default synthetic provider the
actual and forecast carbon intensities come from the same deterministic profile, so they agree by
construction; the code paths are still separate, and only a real provider would show the gap.)

**Impact summary** (`GET /api/impact/summary`) totals the non-aborted sessions:

- a **completed** session saved `baseline − actual`, and is on time when its final SoC reached its
  target (within `settings.soc_tolerance`);
- an **active** session saved `baseline − (actual so far + its remaining planned slots priced with
  the forecast)`, and is on time when the last tick left it no unmet energy.

**Load curve** (`GET /api/sites/{id}/load-curve`) is 96 slots from the site's earliest non-aborted
plug-in. `baseline_kw` is the sum of every session's naive profile. `optimized_kw` is measured for
slots that have already ended — each session's metered energy for the slot divided by the slot
length — and planned for the current and later slots. Both peaks are returned, which is what shows that the coordinated
load stays under the site limit while the naive one would not.

### Endpoints

| Endpoint | What it gives |
|---|---|
| `GET /api/sessions/active` | every active session's columns plus `ocpp_id`, `manual_limit_w`, `projected_unmet_kwh`, `on_time` and its latest 96-slot schedule |
| `GET /api/sessions/{id}/schedule` | `{session_id, computed_at, slots: [{slot_start, power_kw} × 96]}`; 404 if that session has no plan yet |
| `POST /api/sessions/{id}/override` | charge at the car's maximum now: records the manual limit, sends the profile immediately and re-ticks → `{session_id, limit_w, status}`. 404 unknown, 409 not active or the charger is not connected or rejected it |
| `GET /api/sites/{id}/load-curve` | optimized vs baseline kW over 96 slots, both peaks and the site limit |
| `GET /api/impact/summary` | exactly `{co2_saved_kg, cost_saved_inr, sessions_on_time, total_sessions}` |
| `POST /api/optimizer/weights` | `{"alpha" ≥ 0, "beta" ≥ 0}` → sets the weights, re-ticks at once and returns `{alpha, beta, tick}` |
| `POST /api/demo/reset` | stops every live transaction (`RemoteStopTransaction`, up to 3 s), truncates `meter_values`, `schedules` and `sessions` with `RESTART IDENTITY`, clears the in-memory limits and the orchestrator state. Sites, chargers and cached grid data survive |
| `POST /api/demo/scenario/{name}` | runs a scenario in the background (409 if one is running, 404 unknown) |
| `GET /api/demo/status` | `{scenario, running, step, total_steps, events, error}` for the scenario |

As in Phase 2, every POST reads its body as JSON whatever the Content-Type says, so `curl -X POST`
works without `-H`. A missing carbon forecast for an active session's zone returns 503.

### Demo scenario

`evening_rush` is the headline scenario: the demo state is reset, the simulation clock is set to
today 18:30 IST, and six cars — the six `vehicles.json` models on CP001…CP006 — plug in nine
simulated minutes apart (18:30 to 19:15), all leaving at 07:00 the next morning.

```bash
python scripts/demo_scenario.py --scenario evening_rush
sleep 30
curl -s localhost:8000/api/impact/summary
```

The script needs only the standard library, so any Python 3 runs it. It starts the scenario, polls
`/api/demo/status` once a second, prints each plug-in as it happens, and finishes by printing each
site's baseline and optimized peak. It exits 1 if any plug-in fails. The API, the seeded database
and `python simulator/run.py --chargers 6` must all be running.

At `TIME_SCALE=60` the scenario spans about 45 real seconds. What it shows: a **baseline peak of
46.8 kW** (all six cars at full AC power at once) against the **40 kW site limit**, an **optimized
peak of 40.0 kW** that never crosses it, all the charging moved into the 00:00–05:00 IST window
where the carbon intensity is lowest, and about 11–12 kg of CO₂ and about ₹200 saved across the
six cars, with every car still reaching 80 % before 07:00.

## Operator dashboard (Phase 5)

The dashboard is the screen the demo runs on: one page at `http://localhost:5173/` that shows what
the optimizer is doing to the site right now. It is read-only apart from the optimizer-weight
sliders, and it invents nothing — every number and every timestamp on it comes from the API.

### Run it

Start the four pieces in this order (each in its own terminal, from the repository root; the venv
and `npm install` are covered under [One-time setup](#one-time-setup)):

```bash
docker compose up -d --wait                                   # Postgres 5435, Redis 6379
(cd backend && ../.venv/bin/python -m app.seed)               # 1 site, CP001…CP006 (idempotent)
(cd backend && ../.venv/bin/uvicorn app.main:app --port 8000) # API + the OCPP server on 9000
.venv/bin/python simulator/run.py --chargers 6                # the six simulated charge points
(cd frontend && npm run dev)                                  # the dashboard on 5173
```

Open <http://localhost:5173/>. With no cars plugged in the page explains itself and shows the live
carbon forecast; then run the headline scenario and watch it fill (about 45 real seconds):

```bash
.venv/bin/python scripts/demo_scenario.py --scenario evening_rush
```

At `TIME_SCALE=60` one real second is one simulated minute, so the six cars plug in between 18:30
and 19:15 simulated time and the interesting window — the 00:00–05:00 charging burst — arrives
about five real minutes later. The header clock shows where the simulation has got to.

**`VITE_API_TARGET`** overrides the dev server's proxy target (it defaults to
`http://localhost:8000`), so a second backend on another port can be driven from its own dashboard
without editing anything:

```bash
cd frontend
VITE_API_TARGET=http://localhost:18907 npm run dev -- --port 5187 --strictPort
```

It is read in `frontend/vite.config.ts` and applies to both the `/api` and `/health` proxies. The
browser only ever requests relative URLs, so no host is baked into the frontend code.

### What each panel shows

| Panel | Source | What it tells you |
|---|---|---|
| Header | `/api/clock`, `/api/sites`, `/api/grid/latest` | Site, grid zone, site limit, charger count, the simulated clock with its `TIME_SCALE`, the grid-data source badge, and a live/stale dot. An API-down banner appears here rather than a blank page. |
| Impact so far | `/api/impact/summary` | CO₂ saved (kg), ₹ saved, and sessions on time out of the total, against the naive "charge at full power on arrival" baseline. The caption states the projection: active sessions count their remaining plan, so part of the figure is not yet delivered. With no sessions it shows `–`, never a zero dressed up as an achievement. |
| Grid carbon intensity | `/api/grid/latest`, `/api/grid/forecast` | The current carbon intensity, the timestamp of the reading, and where it sits on the next 24 hours' range (cleanest → dirtiest), coloured on the same scale as the Gantt. |
| Optimizer weights | `POST /api/optimizer/weights` | α (carbon) and β (money) for the objective `Σ p·Δt·(α·carbon/1000 + β·tariff)`, plus the status, solve time, session count and timestamp of the tick the change produced. `defaults` restores α 1.00 / β 0.001 — the β slider's 0.005 step cannot return to 0.001 on its own. |
| Charging plan (the Gantt) | `/api/sessions/active`, `/api/grid/forecast` | The hero panel — see below. |
| Site load | `/api/sites/{id}/load-curve` | The optimized site load against the naive baseline, with a red dashed line at the site's `max_power_kw` and the over-limit region shaded. The baseline visibly crosses the limit; the plan does not. |
| Chargers | `/api/sites`, `/api/sessions/active` | The six charge points: OCPP status, the car on them, SoC against target and deadline, the heartbeat age in real seconds, and the limit the CSMS has pushed for the current slot — labelled `planned` or `override`, because it is a commanded setpoint and not a meter reading. A car whose plan gives it 0 kW in the current slot reads `holding · from HH:MM`, not `0.0 kW`, so a deliberate pause never looks like a fault. |
| OCPP 1.6-J frames | `/api/ocpp/log?limit=50` | The raw JSON frames on the wire, newest first, capped at 50, inbound and outbound colour-coded and labelled, each with its time, direction, charge point and the frame verbatim. |

### The Gantt, and its colour scale

`frontend/src/components/ScheduleGantt.tsx` is hand-rolled SVG — no chart library, per the spec.
The X axis is the 96 quarter-hour slots of the horizon, taken from the forecast's own timestamps,
with hour labels every fourth slot.

- **Background:** one `<rect>` per slot, no stroke, drawn at 0.45 opacity so blocks stay legible.
  The fill is a three-stop scale interpolated in RGB — green `#16a34a` at the horizon's lowest
  carbon intensity, amber `#f59e0b` at its mean, red `#dc2626` at its highest. The scale therefore
  spans the horizon's own range, and the legend prints that range in gCO₂eq/kWh.
- **Blocks:** one row per active session, solid slate `#0f172a`, opacity `power_kw / max_charge_kw`
  with a floor of 0.15 so a small power is still visible.
- **Markers:** a vertical "now" line from the simulated clock (labelled below the band, so it never
  covers a row), and a dashed tick at each session's departure deadline — the reason a plan stops
  where it does instead of reaching a cleaner window later in the day. The time after a car's
  departure is greyed out on that car's row ("car has left — not chargeable" in the legend), so it
  is obvious that an empty green window later in the day was simply out of reach.
- **Hover:** any slot shows the slot's local time range, the planned kW and the carbon intensity.

The claim the panel has to make at a glance is "the charging is avoiding the red parts". On
`evening_rush` the reddest stretch is the 18:00–22:00 evening peak, and no block is drawn there;
the blocks sit in the cleanest window every car can still reach before its 07:00 departure. The
genuinely green midday window is after every deadline, which is what the deadline tick explains.

### Live data, polling and time

- `frontend/src/hooks/useLiveData.ts` polls on a timer, **never faster than every 3 seconds** — the
  interval is clamped to a 3 s floor in the hook itself, so no caller can poll harder. The panels
  poll at 3 s; the 24-hour carbon forecast, which advances one 15-minute slot at a time, polls at
  15 s. A request is never started while the previous one is in flight, the timer is cleared on
  unmount, and the last good data stays on screen when a request fails (the header dot turns stale
  and a banner names the failing feeds).
- Moving a weight slider is not polling: the POST is debounced ~300 ms, the backend re-runs the tick
  before it replies, and the page then refetches the four feeds that a re-plan changes so the Gantt
  reshapes within a few seconds.
- There are no animations anywhere, and each panel sits in a fixed-height slot so arriving data
  cannot move the page.
- Every timestamp comes from the API and is simulated time, formatted in `Asia/Kolkata`. The browser
  clock is never read — including for the Gantt's "now" line, which is `GET /api/clock`.

### The "estimated" rule

`GRID_PROVIDER=synthetic` is the default, and its carbon intensities are modelled, not measured. So
wherever a grid value appears with `source === "estimated"` the UI says so and never presents it as
a reading: an amber `GRID DATA: ESTIMATED` badge in the header, an `estimated` chip on the carbon
gauge, and an `ESTIMATED — synthetic grid profile, not measured` note beside the Gantt's legend. If
the provider is ever switched to Electricity Maps, those badges print the real source string
instead. The same honesty applies to power: the charger cards label their kW `planned` or
`override` because it is the limit pushed over OCPP, not metered draw.

### Known limitation — the metered part of the load curve

Slots that have already ended are metered, not planned: per session, the backend takes the energy
its meter register gained during the slot and divides by the slot length, spreading each reading
period across the slots it spans in proportion to the time spent in each. A charge point reports
every 10 real seconds, which at `TIME_SCALE=60` is one reading per 10 simulated minutes against a
15-minute slot, and the plan can switch every 5 simulated minutes — so a reading period straddling a
slot boundary smears energy across it and a past slot can still read a few tenths of a kW above the
40 kW limit (about 1%) even though every commanded limit, and every planned slot, is at or below it. The dashboard shows this honestly — the peak turns red — and
annotates it `(metered — planned peak N kW)` so the plan and the measurement are not confused.

## Driver app (Phase 6)

The phone view lives at <http://localhost:5173/driver> — one page,
`frontend/src/pages/DriverApp.tsx`, built for a 390 px viewport. It runs on the stack the
[operator dashboard](#operator-dashboard-phase-5) already needs; nothing extra has to be started.
It is the same backend, the same simulated clock and the same accounting as the dashboard, cut down
to one driver's car, and it invents nothing: every figure on it is a field an API returned and the
browser only formats (grams → kilograms, watts → kilowatts, fractions → percentages). Since Phase 7
a model assists this screen in two places — it prefills the plug-in form from the driver's own
sentence, and it narrates the plan — but it only ever assists: no number the driver reads comes out
of its text, nothing is submitted on its say-so, and the explanation the browser assembles from the
API's own values, sentence by sentence, is still what renders until (and if) the narration answers.
See [LLM layer](#llm-layer-phase-7). The session id is kept in `localStorage`, so a refresh, or a
phone locking mid-charge, comes back to the same car.

### The four screens

1. **Plug in** — the chargers that are actually free (`status` `Available` with a heartbeat, from
   `GET /api/sites`), the cars from `GET /api/vehicles`, a current-charge slider, a target slider
   (always kept above the current charge) and a departure time. The departure is a time of day: it
   is converted to `hours_until_departure` against the **simulated** clock (`GET /api/clock`) and
   rolls to tomorrow when that time has already gone past today. Submitting calls
   `POST /api/debug/plug-in`, the same endpoint the demo scenarios use. Above those controls is a
   free-text box — "leaving at 7am, need 80%", in English, हिंदी or ગુજરાતી — whose button calls
   `POST /api/llm/extract` and moves the target slider and the departure picker to what the
   sentence said, through the same handlers a finger uses. It prefills and stops there: the driver
   reads both values back in an "Understood: 80% by 07:00" banner and still presses **Start
   charging** themselves. Nothing read, 503 with no key, a 15 s timeout or an offline backend all
   end in the same line, "Couldn't read that — set it below", with the form untouched.
2. **Your plan** — the target and the ETA, a one-row carbon strip (the same green→amber→red scale
   as the operator Gantt, with this car's blocks, a "now" line and a dashed departure line), the
   CO₂ and ₹ saved, and the plain-language explanation. The explanation starts as the browser's own
   sentences and is replaced, once the optimizer has actually given this session a plan, by the
   text `POST /api/llm/explain` returns — asked for once, not on a timer. If that call fails,
   404s, 503s or times out, the browser's sentences simply stay.
3. **Live** — the current power, an SoC ring against the target, the green/grey kWh split and the
   running savings. Polls at the 3 s floor the dashboard uses.
4. **Override** — a red "I'm leaving now" button in a bar pinned to the bottom of both screens, so
   it is reachable without scrolling.

### The three new endpoints

- `GET /api/vehicles` — the list from `backend/app/data/vehicles.json`
  (`{model, battery_kwh, max_ac_kw, max_dc_kw}` per car), so the plug-in form never hard-codes car
  data.
- `GET /api/sessions/{id}/impact` — one session's version of the impact summary:
  `{session_id, co2_saved_g, cost_saved_inr, co2_actual_g, co2_baseline_g, green_kwh, grey_kwh,
  eta, projected_soc_at_deadline, on_time, energy_needed_kwh, energy_delivered_kwh}`.
- `GET /api/sessions/{id}/override-preview` — `{session_id, extra_co2_g, extra_cost_inr, eta_now,
  eta_planned, limit_w}`: what charging at full power from now would **add**, against the plan the
  car is following. It only reads; the override itself is still the Phase 4 POST.

Both per-session endpoints answer 404 for a session that does not exist, and 503 when no carbon
forecast is cached for the site's zone — the same rule as `GET /api/impact/summary`. The savings
follow that summary exactly, so the per-session figures add up to it: a session that has finished
is baseline − actual, an active one is baseline − (delivered + the rest of its plan priced with the
forecast). `eta` is the end of the planned slot at which the plan first covers the energy still
needed; it is `null` when the plan never covers it, and also when no energy is needed any more —
there is no arrival left to wait for.

### Green and grey kWh — a presentation choice, not a measurement

The Live screen splits the energy the car has drawn into "cleaner grid" and "dirtier grid". The
rule is: **metered** energy delivered in a 15-minute slot whose carbon intensity is *below the mean
carbon intensity over that session's own window* (plug-in slot to departure slot) counts as green,
the rest as grey. No grid tells anyone which electrons were renewable; this only says "cleaner than
this car's average hour, or not", and the screen says so in those words. Two consequences worth
knowing: the split uses the cached **forecast** carbon intensity, which under the default
`GRID_PROVIDER=synthetic` is the same deterministic curve the actual accounting uses; and it sums
to what the meter has reported, which is `energy_delivered_kwh` except for the final
StopTransaction reading of a session that has just ended, so on a finished car the two halves can
add up a hair under the delivered figure.

### The override, and what it really does

"I'm leaving now" takes **two taps**, because it costs something and the driver should see what.

1. The first tap only calls `GET /api/sessions/{id}/override-preview` and shows the four numbers it
   returns: the extra CO₂, the extra rupees, the arrival time charging now would give and the one
   the green plan gives. Both sides are priced with the same cached forecast and the same tariff
   the optimizer plans with — nothing is estimated in the browser. "Keep my green plan" sends
   nothing.
2. The second, red tap calls `POST /api/sessions/{id}/override` — the Phase 4 endpoint — which sets
   the session's manual limit to the car's full power in watts and sends the charge point a
   `SetChargingProfile` immediately. The screen then shows the limit the charger accepted.

From that moment the session is out of the linear program (see
[Manual limits](#orchestration-phase-4)): the orchestrator re-sends that limit every tick and never
re-plans the car, its draw is subtracted from the site limit the other cars share, and the app
reads the override back from `manual_limit_w` on `GET /api/sessions/active` rather than remembering
it locally. The site limit still holds — the override takes its kilowatts out of the other cars'
share, not out of the transformer.

### Acceptance at 390 px

The Phase 6 acceptance is visual: on a 390 px viewport, plug a car in, receive a plan, watch it
charge, press the override and confirm in `GET /api/ocpp/log` that the charger jumps to full power
within one tick. One caveat about the tooling: on this machine headless Chrome will not open a
window narrower than about 500 px, so
`--headless=new --window-size=390,844 --screenshot` renders the page at 500 px and saves a 390 px
**crop** of it — a perfectly good layout then looks cut off on the right. For a true 390 px
viewport drive Chrome over CDP with
`Emulation.setDeviceMetricsOverride {width: 390, height: 844, deviceScaleFactor: 2, mobile: true}`.

### Honest states

- **Power now** is a commanded limit, not a metered draw — the override limit while overridden, and
  otherwise the plan's power for the current 15-minute slot. Each is labelled as what it is, the
  same way the dashboard's charger cards are.
- **Savings wait for the plan.** In the few seconds between the plug-in and the first tick a
  session has no schedule, so the API's saving is the whole baseline (there is no remaining plan to
  subtract from it). The screens show `—` and "once the plan arrives" until the first plan lands,
  rather than a figure that would immediately collapse.
- When a feed fails the last good values stay on screen under a "Connection lost" banner, exactly
  like the dashboard.

## LLM layer (Phase 7)

Three narrow capabilities, in `backend/app/llm/` behind `backend/app/routers/llm.py`. Not a
chatbot: each one does a single job, and the whole layer is the last item on the spec's cut list —
the product works with it deleted.

1. **Extract** (`extract.py`) — a driver's own sentence becomes the two values the plug-in form
   asks for: a target state of charge and a departure time. It only ever **prefills** the form;
   the driver reads the values back and submits them.
2. **Explain** (`explain.py`) — a plan that is already on screen becomes two or three plain
   sentences. Narration only.
3. **Copilot** (`copilot.py`) — an operator question is answered from exactly three read-only
   tools, all implemented in our own code: `query_sessions(filters)`,
   `get_site_performance(site_id, days)` and `get_carbon_history(hours)`. There is no fourth tool,
   no SQL from the model, and no free-form tool argument: each tool's arguments are a Pydantic
   model with `extra="forbid"`, the tool name is a whitelist check, and at most two calls run.

### THE RULE, and how it is enforced

> **The LLM never produces a number that appears in the UI.** It receives pre-computed values and
> narrates them. It classifies and extracts. It does not calculate.

This is the spec's rule for Phase 7 and it is enforced structurally, not by asking nicely:

- **The model is never shown raw data.** `explain.py` receives values the optimizer, the providers
  and `orchestrator/accounting.py` already computed, formatted into display strings by our code
  (`"3.2 kg"`, `"₹118"`, `"01:00-04:30 IST"`, `"80%"`) — never an array, a float or a timestamp.
  The router gathers them; `explain.py` cannot reach the database, the providers or the clock at
  all. The copilot's tools likewise return numbers that our code computed, rounded and formatted
  (state of charge as a percentage, times already in site-local form, CO₂ in both g and kg) so the
  model never converts a unit or a timezone. The carbon tool returns a summary of the curve, not
  the curve.
- **The system prompts forbid arithmetic** — no adding, averaging, unit conversion, working out a
  duration, or counting the rows of a list.
- **Every reply is parsed into a Pydantic model and thrown away if it does not validate.**
  `ExtractedConstraints` bounds `target_soc` to 0–1 and `confidence` to three literals; a deadline
  must parse, must be in the future of the *simulated* clock and must be within 14 days.
- **Every number in a finished sentence is checked against the numbers we supplied.** In
  `explain.py` the check is unit-aware: a figure written with a unit must match a fact carrying
  *that* unit, so "saves 118 kg of CO₂ and ₹3.2" is rejected when the computed facts are `3.2 kg`
  and `₹118` — both values were supplied, but neither with the unit it is written in. Lifting the
  digits out of a car name (`XUV400` → "400 kg", with that car on the charger) and inventing
  "999 kg" fail the same way, and so does any number written as a duration ("ready in about
  3 hours"), because no fact is ever a duration and nothing can ground one. A quantity spelled out
  in words is rejected too, by its own rule, since it carries no digits for the check above. The
  copilot does the same against its tool payload. A rejected answer is asked for once more, and
  then loses its turn.
- **Extraction is not calculation.** Reading a stated target or deadline out of "I need 80% by 7am
  tomorrow" is extraction, which the spec allows; the resulting values are bounded and re-emitted
  in site-local time by our code.

Its limits, stated plainly: the grounding check compares values, so a small integer the model
derived itself (say, counting two completed sessions in a list) can still slip through when the
same small integer appears elsewhere in the payload, and the copilot's check does not read numbers
spelled out in words. Both are prompt-forbidden and neither was observed live, but the check is a
backstop, not a proof.

### The sentence to ask for: a car that will miss its target

The easiest thing for a judge to probe, and the one place a narration could lie. `on_time` and
`projected_soc_at_deadline` come out of `accounting.session_impact()` like every other number, and
when `on_time` is false three things change at once — in our code, before the model is called:

- the ETA is dropped from the facts. `accounting` already leaves it `null` for a plan that misses,
  and `build_explain_facts` drops it again, so there is no finish time in the prompt to promise.
- `projected_soc` is set, and *only* then: the charge the plan actually reaches by the deadline.
- the prompt block carries the line `Reaches the charge target by the deadline: NO`, and the system
  prompt requires the **first** sentence to say plainly that the car will not reach the target and
  to give the charge it does reach — never a ready time, never softened into a maybe.

The deterministic fallback says the same thing from the same fields (the `risk_*` templates in
`explain.py`, in all three languages), because that text is what ships when the model call fails.
The `tight_deadline` scenario produces this state on demand.

### Configuration — the key lives in `.env` and nowhere else

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | The only provider this build implements. Any other value is refused with a message naming the supported one — a 503, never a 500. |
| `LLM_API_KEY` | *(empty)* | The Gemini API key for `generativelanguage.googleapis.com`. **`.env` only.** |
| `LLM_MODEL` | `gemini-2.5-flash` | Overridable by env var. Deliberately not in `.env.example`, so a stale `.env` needs no edit. |

**No API key is committed.** `.env` is gitignored and has never been tracked; `.env.example`
carries no key. The key is read in exactly one place — `app.config.settings.llm_api_key`, used only
by `app/llm/client.py` — and is sent as the `x-goog-api-key` **header**, never in a URL or a query
string, so it cannot end up in a log line or a proxy's access log. The client also scrubs the key
out of any provider text before logging it, in case a provider ever echoes it back in an error.

`gemini-2.5-flash` is a pinned name rather than an alias such as `gemini-flash-latest`, so the demo
cannot shift underneath us. If the configured model is not on the key's account, the client asks the
API which models it does offer and says so by name.

### The three endpoints, and the CLI

```bash
# 1. A driver's sentence -> the two form values, or null when it stated nothing readable
curl -s localhost:8000/api/llm/extract -d '{"text":"I need 80% by 7am tomorrow"}'
# {"target_soc":0.8,"deadline_iso":"2026-09-13T07:00:00+05:30","confidence":"high",
#  "detected_language":"English"}

# 2. One session's plan in 2-3 sentences. `language` is optional: "English" | "Hindi" | "Gujarati"
curl -s localhost:8000/api/llm/explain -d '{"session_id":1,"language":"Hindi"}'
# {"text":"...","llm_used":true}

# 3. An operator question, answered from the three tools only
curl -s localhost:8000/api/llm/copilot -d '{"question":"Which cars will miss their deadline?"}'
# {"answer":"...","tools_used":["query_sessions"]}
```

Shapes and limits, from `backend/app/routers/llm.py`: `/extract` takes `text` up to 2000
characters (422 above that, and 413 without reading the body when the declared length exceeds
16 KiB) and answers **200 with `null`** — not a 404 or a 422 — when the sentence stated nothing
readable, because a failed extraction is an ordinary outcome the driver's form absorbs. `/explain`
takes `session_id` and an optional `language` (≤ 40 characters), and answers 404 for an unknown
session and 503 when no carbon forecast is cached for the site's zone — the same rule as
`GET /api/sessions/{id}/impact`. `/copilot` takes a `question` of 1–500 characters and returns the
tool names that actually ran.

`llm_used` on `/explain` is provenance, and it is reported by the code that picked the sentences
rather than guessed afterwards from the string: `true` means the model wrote them, `false` means the
deterministic explanation did. Both are equally true — they quote the same computed numbers — but an
operator (and a judge) is entitled to know which one they are reading.

The spec's acceptance test is the extraction CLI, run from `backend/`:

```bash
python -m app.llm.extract --test "मुझे कल सुबह 7 बजे तक 80% चाहिए"
python -m app.llm.extract --test "leaving at 4am, need full charge"
python -m app.llm.extract --test "asdfgh"     # prints None, exit 0
```

Run on 2026-09-12 they print, in order:

```
{ "target_soc": 0.8, "deadline_iso": "2026-09-13T07:00:00+05:30", "confidence": "high", "detected_language": "Hindi" }
{ "target_soc": 1.0, "deadline_iso": "2026-09-13T04:00:00+05:30", "confidence": "high", "detected_language": "English" }
None
```

The third is the point: gibberish states no constraints, so nothing is extracted and nothing is
invented to fill the gap — and the exit code is 0 either way, so the acceptance test can never fail
on a traceback. With no key configured all three print `None` as well: the CLI calls
`extract_constraints` directly, which skips the call it knows will fail. Over HTTP the endpoint
answers 503 instead, and the driver's form treats the two the same way. Relative times resolve
against the **simulated** clock; `--now <ISO>` overrides it.

### Languages

English, Hindi and Gujarati, in all three capabilities. Hinglish written in Latin script ("bhai
subah 6 baje tak 90 percent kar do") is read as well. `/explain` validates the script of the reply,
not just the label, so a Hindi request that comes back in English is rejected. The `language` field
is mapped through a fixed table (`en`/`hi`/`gu`, the native names; anything unrecognised becomes
English), so a client string is never a free-text channel into the prompt. The deterministic
fallback exists in all three languages too.

### The copilot's three tools

Exactly three, by design — the spec allows no others, and `TOOLS` is the whitelist checked again at
dispatch:

| Tool | Arguments | What it returns |
|---|---|---|
| `query_sessions` | `status` (`active`/`completed`/`aborted`/`any`), `site_id`, `hours` 1–720, `limit` 1–20 (default 5) | `matching_sessions` — the count matching the filters — and up to `limit` rows: vehicle, charger, status, SoC, energy delivered, that session's own savings, its deadline and whether it is on time |
| `get_site_performance` | `site_id` (required), `days` 1–90 (default 7) | one site's totals over whole past days: sessions, sessions on time, energy, CO₂ and rupees saved, peak load, and the `basis` they are measured on |
| `get_carbon_history` | `hours` 1–168 (default 24) | the grid curve already summarised: latest, minimum, maximum, mean, and the greenest and dirtiest times |

Two model round trips per question — one to choose the tools, one to narrate their results — and at
most `MAX_CALLS = 2` tool calls in between. A question none of the three fits is answered with our
own fixed sentence, so even a refusal cannot carry a number. There is **no copilot UI**: Phase 7
adds no screen, the operator dashboard does not call it, and it is demonstrated with the `curl`
above.

### Nothing waits on it, and nothing breaks without it

`client.complete()` returns `None` on every failure there is — no key, an unsupported provider, a
timeout, a connection refused, a 4xx or 5xx, a non-JSON body, a safety block, a truncated reply —
and never raises. Above it:

- **No key configured:** all three endpoints answer **503** in about a millisecond with the reason
  (which setting is missing, and that every other GreenCharge feature works without it). Never a
  500, never a hang.
- **`/extract` fails:** **200 `null`**, and the driver fills the form in by hand. Both attempts
  share a 15 s budget.
- **`/explain` fails:** **200** with the deterministic sentences and `llm_used: false`. A driver
  never waits on a failure: the whole call, retry included, is capped at 8 s, and the fallback text
  is built before the first request goes out.
- **`/copilot` fails:** **503** — it is the one capability with nothing to show without a model.
  Per call 20 s, and the whole question is capped at `TOTAL_BUDGET_S = 45 s`, so a pathological
  request can never leave a demo screen spinning indefinitely. Observed live: 1.3–16.6 s.
- **With the network off** the calls fail on DNS in hundredths of a second, and the rest of the
  product is untouched: the optimizer, the OCPP round trip, the dashboard, the driver app and every
  other endpoint carry on exactly as they do with `GRID_PROVIDER=synthetic`.

Under those budgets sits the transport's own: `DEFAULT_TIMEOUT_S = 12 s` per request with
`CONNECT_TIMEOUT_S = 5 s` to connect (`client.py`), which `extract` and `explain` override with
whatever is left of their 15 s and 8 s. Each is a deadline over the whole call, not a per-read
timeout, so a provider dribbling bytes cannot keep resetting the clock past it. In the browser both
calls carry `LLM_REQUEST_TIMEOUT_MS` = 15 s (`frontend/src/api/client.ts`) — exactly the extraction
budget, so either side may be the one that gives up, and both outcomes leave the same untouched form.

The layer is isolated structurally as well as by configuration: **no module outside
`backend/app/llm/` imports `app.llm` except its own router**. The optimizer, the orchestrator, the
OCPP layer, the accounting and the scenarios cannot reach it, and deleting the key changes nothing
but the three endpoints.

## Decisions & deviations

Decisions (2026-09-11) that refine or deviate from the spec, and known limitations:

- **Placeholder tariff.** `backend/app/data/tariff_gerc.json` is a time-of-day tariff with all 24
  hours explicit, in site-local time (Asia/Kolkata): hours 0–8 = 6.0, 9–16 = 4.8, 17 = 6.0 and
  18–23 = 7.5 INR/kWh, plus a demand charge of 300.0 INR per kVA per month. These are placeholders
  pending confirmation from the GERC tariff order; verify them before presenting.
- **Synthetic grid data** (`GRID_PROVIDER=synthetic`, the spec default), so the project runs without an
  Electricity Maps token. The synthetic provider replays `backend/app/data/carbon_profile_in_we.json`:
  the spec's illustrative daily window table as 96 fifteen-minute slots of Asia/Kolkata time, with
  small deterministic noise, so every day and every run gives the same values. These are estimates,
  marked `"source": "estimated"`.
- **Electricity Maps provider not verified.** `backend/app/providers/electricitymaps.py` follows the
  API shapes in the spec's Appendix A strictly, but it has never been run against the live API,
  because no token is available. Appendix A has no history endpoint, so with this provider
  `/api/grid/history` returns only points cached by earlier `/api/grid/latest` calls, which may be few
  or none. On HTTP 401/403 the API answers 503 with a message to set `GRID_PROVIDER=synthetic`;
  nothing falls back automatically.
- **Clear cached grid data before switching `GRID_PROVIDER`.** The spec's `grid_data` table has no
  provider column, and both providers cache into it, so the Electricity Maps provider would serve
  cached synthetic estimates as Electricity Maps data. Delete the zone's rows before switching:
  `docker compose exec postgres psql -U greencharge -d greencharge -c "DELETE FROM grid_data WHERE zone = 'IN-WE';"`
- **Site limit of 40.0 kW instead of the spec's 150 kW** (user-approved deviation, in the seed data).
  Vehicle AC limits (7.0–11 kW) cap six cars at about 47 kW, so a 150 kW limit would never bind and
  the Phase 5 check "Load curve shows baseline crossing the limit line and optimized staying under"
  could not pass. Chargers stay 6 × 22.0 kW.
- **Extra file `backend/app/routers/sites.py`** (user-approved). It serves `GET /api/sites`; the
  spec's Phase 1 manifest lists only `routers/grid.py`.
- **`vehicles.json` is an object, not a bare list.** `backend/app/data/vehicles.json` is
  `{"verify_specs": true, "vehicles": [...]}`, so it can carry the `verify_specs` marker the spec asks
  for. The six entries are the spec's, verbatim; confirm them against manufacturer sites before
  presenting.
- **Postgres on host port 5435 instead of 5432** (user-approved deviation). The development machine
  runs a native PostgreSQL 17 on 5432 that starts at boot, so the container publishes `5435:5432` and
  `DATABASE_URL` points at `localhost:5435`. Inside Docker it is still `postgres:16-alpine` on 5432.
- **Not built:** Open-Meteo, Open Charge Map, ACN-Data and the map view. The spec mentions them, but they
  are in no phase's file manifest.

Phase 2 (OCPP layer):

- **Extra files** (user-approved): `backend/app/clock.py` (the simulation clock),
  `backend/app/routers/debug.py` (plug-in, set-limit, OCPP log and clock endpoints) and
  `backend/app/routers/sessions.py` (meter values). The spec's Phase 2 manifest lists only
  `backend/app/ocpp/` and `simulator/`. `.env.example` gained one line, `TIME_SCALE=60`.
- **CSMS in the API process**, not a separate process (see
  [Simulator & OCPP](#simulator--ocpp-phase-2)).
- **TIME_SCALE compresses simulated time only.** The battery physics, the departure countdown and
  every timestamp follow it; the Heartbeat, MeterValues, physics-step and reconnect intervals are in
  real seconds.
- **Plug-in relay through `DataTransfer`** (`GreenCharge`/`SimPlugIn`). It is not in the spec's
  table of outbound calls, but OCPP has no other way to tell the simulated charger that a car
  arrived.
- **StartTransaction without a pending plug-in creates no session.** A session needs the vehicle,
  battery, target and deadline, which StartTransaction does not carry, so the CSMS replies with
  idTagInfo `Invalid` and transaction id 0, and the charger does not charge.
- **Simulator extras beyond the spec's list.** Before StopTransaction the charger sends one last
  MeterValues (context `Transaction.End`, power 0 W, final energy and SoC), so the session's final
  SoC is recorded and its last meter-value row has `power_kw` 0.0. After every BootNotification it
  reports connector 1's status. The physics step in which the car reaches its target or departs is
  cut short at that moment.
- **OCPP state lives in memory** in the API process: the simulation clock, the connected chargers
  and manual limits. Restarting the API (including a `--reload`) forgets them. The chargers
  re-register when they reconnect and keep their last limit, and the clock starts again at the real
  UTC time. Readings taken after a restart can therefore carry earlier timestamps than those before
  it; meter values stay in arrival order, so `tail -1` is still the newest reading.
- **Sessions left `active`.** If a charger never sends StopTransaction, for example because the
  simulator was restarted in the middle of a session, that session stays `active`, and plug-in on
  that charger answers 409 until it is cleared. From Phase 4 the way to start clean is
  `curl -X POST localhost:8000/api/demo/reset`, which stops the live transactions first. Without the
  API running, empty the tables by hand (this also empties `meter_values` and `schedules` and
  restarts session ids at 1):
  `docker compose exec postgres psql -U greencharge -d greencharge -c "TRUNCATE sessions RESTART IDENTITY CASCADE;"`

Phase 4 (orchestration):

- **Extra files** (user-approved): `backend/app/routers/impact.py` (`/api/impact/summary` and
  `/api/optimizer/weights`), `backend/app/routers/demo.py` (the reset and scenario endpoints),
  `backend/app/scenarios.py` (the scenario definitions, the runner and the reset) and
  `scripts/demo_scenario.py`. The spec's Phase 4 manifest lists only `backend/app/orchestrator/`,
  but its acceptance test runs `scripts/demo_scenario.py`, which needs a scenario to run.
  `backend/app/routers/debug.py` was refactored so the scenarios can reuse the plug-in without
  going through HTTP; both debug endpoints behave exactly as before.
- **Known limitation of the MPC tick.** Slot 0 is the *current* slot, which is already partly
  elapsed, but the linear program credits it with a full 15 minutes, so a plan can promise slightly
  more energy in the current slot than the car can still take in it. The plan is rebuilt every tick,
  so the error is corrected long before it matters; it only shows up in plans that are tight right
  at the deadline. This is deliberately **not** fixed by changing the model — a partial first slot
  would complicate every constraint for a rounding-scale gain.
- **Savings for a session in progress are an estimate.** An active session is credited with its
  baseline minus (what it has actually used plus what it still plans to use, priced with the
  forecast). Energy that is planned but never delivered — a charger that faults, a plan the solver
  had to relax — therefore shows as saving until the session ends. Completed sessions are measured,
  not estimated.
- **The load curve counts only active sessions in the future.** For slots that have not ended, the
  optimized line sums the latest plan of the sessions that are still `active`. A completed session's
  last plan is left out, because that car has gone; it is already represented by its metered power in
  the slots that have ended.
- **Past slots on the load curve are limited by metering resolution.** A past slot's optimized value
  integrates each car's metered energy over the slot (reading periods are split across slot
  boundaries by time). At `TIME_SCALE=60` a car reports once per 10 simulated minutes while the plan
  can change every 5, so a straddling reading period smears energy between slots and the optimized
  peak can read roughly 1% above the site limit even though no instant exceeded it. The plan itself
  (and every limit actually sent) always respects the limit.
- **`StatusNotification(Available)` aborts a lost session only on a real connector.** A charge point
  reporting `Available` on connector 1 while a session is still active has lost that transaction (the
  simulator restarted, say), so the session is marked `aborted`. Connector 0 is excluded: in OCPP 1.6
  it reports the charge point's main controller, not a connector, so it says nothing about a
  transaction. The simulator only ever uses connector 1.
- **The load-curve window starts at the earliest non-aborted plug-in** of the site and is 96 slots
  long, as specified. A session older than 24 hours that was never stopped therefore pushes "now"
  off the right-hand edge of the chart. `POST /api/demo/reset` (which every scenario runs first)
  clears it.
- **One WARNING burst is expected right after a reset.** A tick that was already in flight when the
  reset stopped the transactions still sends its (0 W) profiles, and the chargers answer `Rejected`
  because their transactions have just ended. It is logged and harmless.

Phase 6 (driver app):

- **One new file**, `frontend/src/pages/DriverApp.tsx` (user-approved). The spec gives Phase 6 no
  file manifest. The backend added no file at all: `GET /api/vehicles` went into
  `backend/app/routers/debug.py`, the two per-session endpoints into
  `backend/app/routers/sessions.py`, and the numbers behind them into
  `backend/app/orchestrator/accounting.py`. `frontend/src/App.tsx` gained the `/driver` route and
  `frontend/src/api/client.ts` the four calls. No new npm package, no service worker, no offline
  caching and no auth — the spec's "smaller than Phase 5".
- **The green/grey split is a presentation choice**, not a measurement, and it is scored with the
  cached forecast carbon intensity (see [Green and grey kWh](#green-and-grey-kwh--a-presentation-choice-not-a-measurement)).
  Under the default `GRID_PROVIDER=synthetic` the forecast and the actual curve are the same
  deterministic profile, so the two agree; with a provider whose forecast differs from what
  happened, a delivered slot older than the oldest cached forecast point is scored with the
  nearest cached value.
- **The driver's "power now" is a commanded limit, not a meter reading.** The streaming meter feed
  (`GET /api/sessions/{id}/meter-values`) is not consumed by the phone view; it shows the override
  limit while overridden and the plan's current slot otherwise, labelled either way.
- **Headless Chrome cannot open a 390 px window on this machine** — `--window-size=390,844` renders
  at about 500 px and crops. Use CDP `Emulation.setDeviceMetricsOverride` for a true 390 px check.
- **A slow plug-in can surface as a network error.** The frontend's request timeout is 10 s
  (`REQUEST_TIMEOUT_MS`, shared by every call since Phase 5) while the backend allows the plug-in
  handshake 15 s, so a plug-in that takes longer than 10 s but still succeeds reaches the app as a
  timeout rather than the server's own message. After a timeout — never after a refusal such as the
  409 for a busy charger — the form looks for an active session on the same charger, with the same
  car and the same starting charge, and adopts it instead of stranding the driver.

Phase 7 (LLM layer):

- **The spec's Phase 7 manifest is backend-only**, and the five `backend/app/llm/` modules plus the
  approved `backend/app/routers/llm.py` are all of it. The driver app also gained a "say it in your
  own words" box and a narrated plan, which touched `frontend/src/pages/DriverApp.tsx` and
  `frontend/src/api/client.ts` — two existing Phase 6 files, no new file and no new package. Both
  uses are assistive: extraction prefills the form the driver still submits, and the narration
  replaces sentences, never the numbers on the tiles.
- **The copilot's savings and the dashboard's headline are measured on different bases**, and they
  differ while a car is still charging — both are our own numbers, but they answer different
  questions. The copilot's tools report each session's **whole naive-charging baseline** minus what
  its meters have actually reported, so a session mid-charge contributes a baseline for charging it
  has not done yet; `GET /api/impact/summary`, which the dashboard renders, also nets off the
  charging still *planned* in the remaining slots. Measured live on one session: 13.1 kg from the
  copilot against 2.9 kg on the dashboard. Each tool result carries a `basis` field saying exactly
  what its figure measures, and the answer prompt requires the copilot to qualify any saving it
  quotes the way that basis describes it, so the two can be reconciled on screen. Reconciling the
  *numbers* is a product decision and has not been taken: `get_site_performance` deliberately does
  not depend on a cached forecast, which is what netting off the remaining plan would require.
- **`LLM_MODEL` is not in `.env.example`.** It defaults in `config.py`, so an existing `.env` needs
  no edit; set it only to pin a different model.
- **The phone view always narrates in English, and ignores `llm_used`.** `/api/llm/explain` accepts
  `language`, and both the model path and the deterministic fallback are written in all three
  languages, but `postLlmExplain` does not send the field yet. Extraction already reads all three
  and names the one it read in the "Understood" banner.
- **The copilot ships without a screen.** It is complete and exercised by hand over HTTP; Phase 7
  deliberately adds no UI, so a judge sees it through `curl` rather than on the dashboard.
- **The number-grounding check is deliberately strict**, so an answer that reaches for a figure we
  did not supply is replaced by the deterministic text (on `/explain`) or refused with a 503 (on
  `/copilot`) rather than shown. That is the rule working, and it occasionally costs a livelier
  sentence.

## Phase status

- Phase 0 — Scaffold: acceptance passed (`/health` reports db and redis true; page reads "GreenCharge")
- Phase 1 — Data layer: acceptance passed (forecast has 96 slots with a 291 g/kWh spread; 6 chargers)
- Phase 2 — OCPP layer: acceptance passed (6 chargers connected; a remote 5000 W SetChargingProfile throttles the car from 7.2 kW to 5.0 kW)
- Phase 3 — Optimizer: acceptance passed (3/3 fixtures; site-constrained solve ≈ 25 ms against the 2 s limit)
- Phase 4 — Orchestration: acceptance passed (`evening_rush`: 11.86 kg CO₂ saved, ₹201 saved, 6/6
  sessions on time, optimized peak 40.0 kW against a baseline peak of 46.8 kW and a 40 kW site limit;
  the commanded limit is 0 W through the evening peak and the energy lands in 00:00–05:00)
- Phase 5 — Operator dashboard: acceptance passed (checked on screenshots of a live `evening_rush`
  run: no blocks in the red evening band, the baseline crossing the 40 kW limit line while the plan
  stays on it, CO₂ and ₹ savings non-zero, live OCPP frames, and the α/β sliders visibly reshaping
  the plan). See [Operator dashboard](#operator-dashboard-phase-5).
- Phase 6 — Driver PWA: acceptance passed (at a 390 px viewport: plug in, receive a plan, watch it
  charge, then override — a car the optimizer had paused at 0 kW was commanded to its full 11 kW
  within one tick, with the other five cars left paused and the site limit still respected). See
  [Driver app](#driver-app-phase-6).
- Phase 7 — LLM layer: built and reviewed; the acceptance result is the orchestrator's to record.
  The spec's three `python -m app.llm.extract --test` commands were run live against
  `gemini-2.5-flash` and returned `0.8 / 2026-09-13T07:00:00+05:30 / high / Hindi`,
  `1.0 / 2026-09-13T04:00:00+05:30 / high / English` and `None` (exit 0, no traceback), and all
  three endpoints answered on a live stack. See [LLM layer](#llm-layer-phase-7).
