# RUNBOOK: start the MOIL / OreSight stack (Windows; run from the repo root unless noted)

Ports: Postgres 5432 · Neo4j 7474/7687 · API 8000 · frontend 5173.
Use curl.exe in PowerShell (plain curl is an alias). Quick start was executed end to end on 2026-09-23 and again on
2026-09-24 (Windows 11, PowerShell; the second time as a cold start from `docker compose down`, after the Stage 4 model
swap; Docker Desktop was already running, so step 0 was not re-tested). Timings below are measured.
Lines marked **INFERRED** were NOT executed or found documented; double-check them.

## Quick start (already set up once)

```powershell
# 0. Start Docker Desktop first (Start menu) and wait until it reports "Engine running".
#    Check with:  docker info    (an error like "cannot connect to the docker API" = it is not up yet)
cd oresight-backend
docker compose up -d          # Postgres + Neo4j + api container; from a fully downed stack allow ~100-130 s (measured 99 s and 131 s on 09-24; 33 s on 09-23) -- Neo4j's healthcheck is the slow part, it has not hung; ~1 s if already up
docker compose ps             # wait until postgres and neo4j show (healthy); api answers /health ~3 s after start
```

```powershell
# Only if the demo DB is empty/dirty (after tests, or first run). ~57 s (measured 2026-09-24; 13 steps; the last one clears leftover test risk events)
# (includes seeding blast_events and the equipment_status_log backfill and a cleanup of leftover test risk events; no separate step)
venv\Scripts\python -m scripts.rebuild_demo_db
```

```powershell
# Only after backend code changed: the api container does NOT pick up code by itself
docker compose build api      # ~4 s with cached layers; minutes after code/dependency changes (the image unpack alone took ~2 min once)
docker compose up -d --no-deps api   # recreates the container with the new image; ~3 s (build alone does NOT swap it)
# The api container mounts the repo's data/ folder at /data (MOIL_DATA_DIR=/data in docker-compose.yml): the Simulator reads
# the satellite rainfall CSV from it and the image cannot see repo-root data/ by itself. If POST /simulate answers 503
# "Live model inputs are unavailable", check:  docker exec oresight-api ls /data
```

```powershell
# Alternative to the api container: local API with reload (frees port 8000 first)
docker compose stop api       # both bind 8000; stopping is a precaution (a clash with the running container was not tested)
venv\Scripts\uvicorn app.main:app --reload --port 8000   # healthy ~4 s; stop with Ctrl+C. A plain process kill leaves the
                              # reload worker holding 127.0.0.1:8000; if so: taskkill /PID <pid> /T /F
# back to the container afterwards: docker compose start api
```

```powershell
cd ..\oresight-frontend
pnpm dev                      # http://localhost:5173, ready in ~5 s; blocks this terminal (use its own); real API by default
```

## First-time setup (fresh clone)

```powershell
git clone https://github.com/Arnabh-M/moil-reserve-intelligence.git   # then: cd moil-reserve-intelligence
cd oresight-backend
python -m venv venv           # the Docker image is Python 3.11 (3.11.16); the dev venv here is 3.14.6 (338 tests pass under it)
venv\Scripts\activate
pip install -r requirements.txt       # backend deps; ~2-3 min (INFERRED)
copy .env.example .env        # backend env (defaults already match docker-compose.yml)
docker compose up -d --build  # builds the Postgres+PostGIS+pgvector image and api; several min first time
docker compose ps             # wait for (healthy)
python -m scripts.rebuild_demo_db                  # alembic + seeds + Neo4j load + scenarios + blast events + equipment history; ~57 s (measured on an existing DB)
cd ..\oresight-frontend
pnpm install                  # Node v24 / pnpm 12.3.4 (packageManager pin); ~1 min (INFERRED)
pnpm dev
```

## Verify it's working

```powershell
curl.exe http://localhost:8000/health          # {"status":"ok",...,"db":"connected","neo4j":"connected"}
curl.exe http://localhost:8000/sites           # 3 sites; centroids = data/moil_sites.json box midpoints
curl.exe http://localhost:8000/reserve-zones   # GeoJSON, 12 features, each with confidence_score
curl.exe http://localhost:8000/equipment/metrics?site_id=1   # 200 = api image is current (stale image lacks it)
curl.exe -X POST http://localhost:8000/simulate -H "Content-Type: application/json" -d '{\"scenario_type\":\"equipment_down\",\"site_id\":1,\"duration_days\":5}'
#   200; after.production_forecast_tonnes < before's; model_state_as_of = the latest FINISHED day with a complete satellite
#   rain record (1-3 days ago); model_inputs_missing normally lists soil_moisture_m3m3 (SMAP lags the rain record)
docker exec -it oresight-postgres psql -U oresight -d oresight -c "\dx"   # postgis + vector installed
```

- Neo4j Browser: http://localhost:7474 · Swagger: http://localhost:8000/docs
- Frontend: open http://localhost:5173, then /map (zones + heatmap) and /site/1. Verified: the page calls
  `GET /sites` and `GET /reserve-zones` on :8000 (both 200), 12 zones, no page errors, no mock.
- Equipment performance: the Dashboard has a "Fleet availability" card (`GET /equipment/metrics`, no site_id) and
  /site/1?tab=equipment shows the per-machine table with a 30/90/180-day toggle and a detail drawer
  (`GET /equipment/{id}/metrics`). If /equipment/metrics fails, the card shows "—" ("metrics unavailable") instead of
  breaking the Dashboard; that usually means a stale api image (see Quick start). **INFERRED**: not re-run since the 09-24 check.
- Optional end-to-end check (needs api + `pnpm dev` up): `cd oresight-frontend; pnpm test:smoke` (26 tests since the
  Equipment-tab tests were added; was 21, ~1.7 min). On a `pnpm dev` that was started seconds ago, the "first paint"
  preflight can fail once while Vite warms up (it waits 1.5 s); re-run it (2026-09-24: 20 of 21 on the first run, 21 of 21
  on the re-run). **INFERRED**: the 26-test count comes from `npx playwright test --list`; the full suite was not run after the change.

## Env vars (names only)

- `oresight-backend/.env` (template `.env.example`): `APP_ENV`, `DATABASE_URL`, `NEO4J_URI`, `NEO4J_USER`,
  `NEO4J_PASSWORD`, `CORS_ORIGINS` (currently ignored by main.py, see "Deploying the frontend"), `PRODUCTION_EDIT_WINDOW_HOURS`, `EQUIPMENT_FLAP_WINDOW_HOURS`, `EQUIPMENT_FLAP_THRESHOLD`.
- `oresight-frontend/.env.local` (template `.env.example`; optional, defaults are the real API): `VITE_API_BASE_URL`, `VITE_USE_MOCK`.
  Unset/empty `VITE_API_BASE_URL` falls back to http://localhost:8000 (no trailing slash when set). `VITE_USE_MOCK`
  unset/`false` (default) hits the real backend; `true` is an offline fallback with mock data (Equipment tab included).
  Both are inlined at BUILD time by Vite: changing them needs a rebuild/redeploy, not just a restart of a served build.
- `.gitignore` now ignores every `.env.*` except `.env.example`, so `.env.production` etc. are never committed.
- `gee_pipeline/.env` (template `.env.example`, GEE steps only): `EE_PROJECT`, `EE_SERVICE_ACCOUNT_JSON`.
- `MOIL_DATA_DIR` (optional): where the backend finds the repo's `data/` folder (default: repo-root `data/`). Set to `/data`
  by docker-compose.yml for the api container.

## Other commands

```powershell
cd oresight-backend; venv\Scripts\python -m pytest -q        # full suite (~85 s, 338 passed, 0 skipped/xfailed, 2026-09-24); WRITES to demo DB
cd oresight-backend; venv\Scripts\python -m scripts.simulator_sweep --old-rev 1084074   # every-day direction check of the Simulator, new vs old model (~4 min)
cd oresight-frontend; pnpm typecheck; pnpm build             # tsc + vite build (~40 s)
```

## Before a demo: re-date the data (production = Render API + Supabase + Aura)

The demo data ends 2026-09-22 and every "last N days" view is wall-clock based, so production goes empty as time passes.
`oresight-backend/scripts/refresh_demo_dates.py` shifts the whole timeline forward by one constant offset so the newest
production record lands on yesterday (UTC), keeping spacing and the scenario stories. Run from `oresight-backend/`.

1. **Back up first.** Dump every row of `production_records`, `blast_events`, `equipment_status_log`, `equipment`, `risk_events`
   (Supabase) and the `WeatherEvent`/`BlastPlan`/`Equipment`/`RiskEvent` nodes with all properties (Aura) into a git-ignored
   `backups/<date>-pre-refresh/` (the root `.gitignore` ignores `backups/`), and note the row counts to compare with step 3.
2. **Point the script at Supabase for this process only** (`.env.supabase` is git-ignored; never put the URL in a committed file).
   Aura comes from `NEO4J_*` in `oresight-backend/.env`:
   ```powershell
   Get-Content .env.supabase | ForEach-Object { if ($_ -match '^SUPABASE_DATABASE_URL=(.*)$') { $env:DATABASE_URL = $Matches[1].Trim('"').Trim("'") } }
   ```
3. **Dry run** (default; read-only; prints masked hosts, the offset, and per-table row counts with old -> new date ranges):
   `venv\Scripts\python -m scripts.refresh_demo_dates --dry-run`. Check the hosts are not localhost and the counts match the backup.
4. **Apply** (one pass: Postgres transaction + Neo4j transaction, Postgres commits last): `venv\Scripts\python -m scripts.refresh_demo_dates --apply`.
   Same-day re-runs are no-ops (offset 0); a later run shifts by the days elapsed. `--rehearse` writes and rolls back, local targets only.
5. **Verify** (Render free tier may cold-start; allow ~1 min):
   ```powershell
   curl.exe https://oresight-api.onrender.com/health                              # status ok, db + neo4j connected
   curl.exe "https://oresight-api.onrender.com/production?site_id=1&days=30"      # 30 rows; last date = yesterday (UTC)
   curl.exe https://oresight-api.onrender.com/kpi/summary                         # twin_last_updated = yesterday 23:59:59Z
   ```
   Also `/risk-events/5/causal-graph` and `/risk-events/11/causal-graph` should report `graph_source: neo4j`.

Notes: Aura (`neo4j+s://`) can fail on Windows machines with TLS inspection (antivirus/proxy) with `SSLCertVerificationError:
self-signed certificate in certificate chain`. The script injects `truststore` (OS certificate store) automatically when it is
importable: it is installed in the dev venv but not pinned in `requirements.txt`, so `pip install truststore` if it is missing.
Rows whose shifted time would land in the future are capped at yesterday 23:59:59Z (reported as `capped=` per table). Not shifted:
`created_at`/`updated_at` columns (so the production edit window is unaffected), `data/satellite_daily_features.csv` and the
frontend satellite JSON / NDVI tiles (real observations), and `site_notes`/`shift_plan_entries`/`reserve_zones`.

## Deploying the frontend (Vercel)

`oresight-frontend/vercel.json` sets framework `vite`, output `dist/public` (what `pnpm build` writes), and rewrites every
path except `/assets/*` to `index.html` so deep links like /site/1 survive a reload.

- Set the Vercel project's Root Directory to `oresight-frontend` (**INFERRED**: vercel.json lives there; project settings are not in git).
- Set `VITE_API_BASE_URL` (the public backend URL) in the Vercel env BEFORE the build; it is baked into the bundle.
- CORS: the backend currently hardcodes `allow_origins=["http://localhost:5173", "https://oresite.vercel.app", "*"]` (credentials on) in
  `oresight-backend/app/main.py` and **ignores** the `CORS_ORIGINS` setting / env var, so setting it changes nothing: every origin is
  allowed today. Wiring `main.py` to `settings.CORS_ORIGINS` (and dropping `"*"`) is a pending code change, not done yet.
  Production is the Render API (https://oresight-api.onrender.com) on Supabase Postgres + Aura Neo4j; hosting config is not in the repo.
- Local check of the production bundle: `cd oresight-frontend; pnpm build; pnpm serve` (vite preview on :5173; if `pnpm dev` holds 5173, preview moves to the next free port; read the URL it prints).

## Optional, retraining the shortfall forecaster (not needed to run the app)

```powershell
cd oresight-backend
venv\Scripts\pip install -r requirements-train.txt    # adds scikit-learn on top of requirements.txt; ONE xgboost dist only (xgboost-cpu==2.1.4)
cd ..
oresight-backend\venv\Scripts\python train_shortfall_model.py          # evaluates, writes models\candidate\ (gitignored); ~15 s
oresight-backend\venv\Scripts\python train_shortfall_model.py --ship   # also installs over models\ if the ship gate passes
```

Retraining is deterministic: on 2026-09-24 a retrain from `requirements-train.txt` alone reproduced the candidate pickle byte for byte.
Never `pip install xgboost` next to `xgboost-cpu`: both write the same `xgboost/` files (a test fails if both are present).

## Optional, data regeneration only (not needed to run the app)

GEE / satellite / prospectivity / tiles (`gee_pipeline/`, `prospectivity/`, `gis/`): see HANDOFF.md, section "GEE setup".

## Known gotchas

- The API image needs `docker compose build api` (+ recreate) after backend code changes, or it serves stale routes.
- The backend test suite dirties the demo DB: run `rebuild_demo_db` afterwards (it now includes the equipment-history
  backfill; rebuild before too, if a previous run left flip history behind). `scripts.export_contract` dirties it too.
  Note rebuild does NOT delete `blast_events` rows it did not seed (hand-entered or leaked-test rows stay). Four leaked
  test rows (ids 298, 412, 413, 433) were deleted by hand on 2026-09-24; three untagged rows (29-31) are intentional demo
  entries. Check with: `SELECT id, notes FROM blast_events WHERE notes IS NULL OR notes NOT LIKE 'synthetic:%'`.
- `pnpm typecheck` needs `pnpm install` first (typescript is now a devDependency).
- Map in production builds: MapLibre's worker is bundled via `?worker&url` in MineMap.jsx plus `worker.format: 'es'` in
  vite.config.ts; without both, the built map stays blank (the worker 404s under /assets/). Every map text layer must set
  `text-font` to `LABEL_TEXT_FONT` (`Noto Sans Regular`, src/lib/map.js): MapLibre's default font stack 404s on the
  OpenFreeMap glyph server and labels silently never draw. `pnpm dev` can hide both problems, so check maps with a build.
- GEE steps need `gee_pipeline/.env` with `EE_PROJECT` and `EE_SERVICE_ACCOUNT_JSON`; keep the key file OUTSIDE git.
- Zone confidence scores (~0.20-0.41) are correct and intentionally low; see HANDOFF.md.
- The Simulator's "before" state is the latest finished day with a complete observed rain record, usually 1-3 days ago
  (`model_state_as_of` in the response, shown in the UI). Nothing is filled in: a value no source has is passed as missing.
- Weather watcher: every 5 min (forecast cached 15 min) the api container asks Open-Meteo for the next 72 h per site; a worst
  24 h of >= 35 mm raises `weather_advisory_rain`, >= 64.5 mm `weather_heavy_rain`, and each auto-resolves when the forecast
  clears. It needs internet from the container; on failure it logs a warning and creates nothing. The test suite never calls
  it (tests/conftest.py) and deletes any `weather_*` risk events it creates; rebuild_demo_db does not delete them.
