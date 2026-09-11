# GreenCharge

GreenCharge is an EV Charging Network Renewable-Optimization Platform, built for HackOut'26 at
Dhirubhai Ambani University. It is built one phase at a time against the project's build
specification (`BUILD_SPEC.md`, the single source of truth, kept outside this directory); a phase
starts only after the previous phase's acceptance test passes. So far it has Phase 0 (a running
skeleton) and Phase 1 (the data layer: database schema, seed data, and grid carbon-intensity, tariff
and site data over HTTP). There is no optimizer, OCPP server or dashboard yet.

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
   still holds 1 site and 6 chargers. A re-run also resets each charger's status to `Available`. The
   API creates missing tables at startup too, but only the seed script inserts the site and chargers.

3. Frontend dev server (port 5173), from `frontend/` in another terminal:

   ```bash
   cd frontend
   npm run dev
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

## Phase status

- Phase 0 — Scaffold: acceptance passed (`/health` reports db and redis true; page reads "GreenCharge")
- Phase 1 — Data layer: acceptance passed (forecast has 96 slots with a 291 g/kWh spread; 6 chargers)
