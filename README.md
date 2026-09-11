# GreenCharge

GreenCharge is an EV Charging Network Renewable-Optimization Platform, built for HackOut'26 at
Dhirubhai Ambani University. It is built one phase at a time against the project's build
specification (`BUILD_SPEC.md`, the single source of truth, kept outside this directory); a phase
starts only after the previous phase's acceptance test passes. Only Phase 0 exists so far: a running
skeleton with no business logic.

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

2. Backend API (port 8000), from `backend/` using the venv:

   ```bash
   cd backend
   source ../.venv/bin/activate
   uvicorn app.main:app --reload --port 8000
   ```

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

## Decisions & deviations

User decisions (2026-09-11) that refine or deviate from the spec:

- **Placeholder tariff** (takes effect in Phase 1). `backend/app/data/tariff_gerc.json` is a
  time-of-day tariff with all 24 hours explicit: hours 0–8 = 6.0, 9–16 = 4.8, 17 = 6.0 and
  18–23 = 7.5 INR/kWh, plus a demand charge of 300.0 INR per kVA per month. These are placeholders
  pending confirmation from the GERC tariff order; verify them before presenting.
- **Synthetic grid data** (`GRID_PROVIDER=synthetic`, the spec default), so the project runs without an
  Electricity Maps token. The Electricity Maps provider (Phase 1) will be written strictly against the
  API shapes in the spec's Appendix A and cannot be verified against the live API, because no token
  is available.
- **Site limit of 40.0 kW instead of the spec's 150 kW** (user-approved deviation; takes effect in the
  Phase 1 seed data). Vehicle AC limits (7.0–11 kW) cap six cars at about 47 kW, so a 150 kW limit would
  never bind and the Phase 5 check "Load curve shows baseline crossing the limit line and optimized
  staying under" could not pass. Chargers stay 6 × 22.0 kW.
- **Postgres on host port 5435 instead of 5432** (user-approved deviation). The development machine
  runs a native PostgreSQL 17 on 5432 that starts at boot, so the container publishes `5435:5432` and
  `DATABASE_URL` points at `localhost:5435`. Inside Docker it is still `postgres:16-alpine` on 5432.
- **Not built:** Open-Meteo, Open Charge Map, ACN-Data and the map view. The spec mentions them, but they
  are in no phase's file manifest.

## Phase status

- Phase 0 — Scaffold: acceptance passed (`/health` reports db and redis true; page reads "GreenCharge")
