# GreenCharge

GreenCharge is an EV Charging Network Renewable-Optimization Platform, built for HackOut'26 at
Dhirubhai Ambani University. It is built one phase at a time against the project's build
specification (`BUILD_SPEC.md`, the single source of truth, kept outside this directory); a phase
starts only after the previous phase's acceptance test passes. So far it has Phase 0 (a running
skeleton), Phase 1 (the data layer: database schema, seed data, and grid carbon-intensity, tariff
and site data over HTTP) and Phase 2 (the OCPP layer: an OCPP 1.6J server inside the API process and
six simulated chargers that obey remote power limits). There is no optimizer or dashboard yet.

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
`GRID_PROVIDER` defaults to `synthetic`.

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
  that charger answers 409 until it is cleared. To start clean, stop the API and the simulator,
  then empty the sessions. This also empties `meter_values` and `schedules` and restarts session ids
  at 1:
  `docker compose exec postgres psql -U greencharge -d greencharge -c "TRUNCATE sessions RESTART IDENTITY CASCADE;"`

## Phase status

- Phase 0 — Scaffold: acceptance passed (`/health` reports db and redis true; page reads "GreenCharge")
- Phase 1 — Data layer: acceptance passed (forecast has 96 slots with a 291 g/kWh spread; 6 chargers)
- Phase 2 — OCPP layer: acceptance passed (6 chargers connected; a remote 5000 W SetChargingProfile throttles the car from 7.2 kW to 5.0 kW)
