# CLAUDE.md

FrogCore is a Cloudflare Python Worker (Pyodide, FastAPI) that backs a photogrammetry-style pipeline: users sign up, upload footage ("surveys") to R2, trigger a Modal build job that produces a `model.glb`, and view it in a three.js page.

`client/` (Cyclops uploader, `frogcore_client.py`) and `modal/` (build job, `build.py`) are separate components and are not documented here; this file covers the Worker, schema and static frontend.

## Commands

Dependencies are declared in [pyproject.toml](pyproject.toml) (`fastapi`) and installed by `pywrangler` (from `workers-py`, run through `uv`). Plain `wrangler deploy` does not bundle them. No test suite or linter configured.

- Deploy: `uv run pywrangler deploy`
- Local dev: `uv run pywrangler dev` (secrets for local runs go in an untracked `.dev.vars`)
- API docs (Swagger UI) are served at `/api/docs`
- Apply schema locally: `npx wrangler d1 execute frogcore --local --file=schema.sql` (schema is already applied in production; there is no migration tooling, so schema changes need manual `d1 execute`)
- Worker secrets (`wrangler secret put`): `MODAL_URL`, `MODAL_KEY`, `MODAL_SECRET`, `CALLBACK_SECRET`

## Architecture

Config: [wrangler.jsonc](wrangler.jsonc). Entry `src/entry.py`, `python_workers` compat flag, bindings `DB` (D1 `frogcore`) and `footage_bucket` (R2 bucket `footage`). Static assets are served from `public/`; `run_worker_first: ["/api/*"]` means only `/api/*` hits the Worker.

Python source in `src/` (flat imports, e.g. `from services import ...`):

- [entry.py](src/entry.py): `Default(WorkerEntrypoint)`. Sends streaming uploads to `uploads.py`; everything else goes to FastAPI through the Workers ASGI adapter (`workers.asgi.fetch`).
- [app.py](src/app.py): the `FastAPI` app: all routes, auth dependencies and error handlers. Every error is `{"error": message}` (the frontend and Cyclops rely on it), including 404/405 and validation failures (400).
- [services.py](src/services.py): `Services` (bindings + repositories, built per request from `request.scope["env"]`), token extraction/authentication, ownership check and the R2 key prefix. Shared by `app.py` and `uploads.py`.
- [uploads.py](src/uploads.py): `PUT /api/surveys/:id/files/*path`, handled on the raw Worker request. The ASGI adapter reads the whole request body into memory before calling the app, which would break 100 MB uploads, so this route bypasses FastAPI (and therefore isn't in `/api/docs`).
- [security.py](src/security.py): PBKDF2-SHA256 via WebCrypto (100k iterations = Workers max), session tokens (only the SHA-256 is stored), constant-time compare, `js_object()` for Python dict -> JS object.
- [store.py](src/store.py): `Database` wrapper over D1 plus `UserRepository`, `SessionRepository`, `SurveyRepository`. All SQL lives here.
- [schema.sql](schema.sql): `users`, `sessions`, `surveys` tables.

### API

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/signup`, `POST /api/login` | no | Create account / log in; return `{token}` and set `session` cookie. Login accepts JSON or HTTP Basic (used by Cyclops) |
| `POST /api/logout` | yes | Delete session, clear cookie |
| `GET/POST /api/surveys` | yes | List / create (uuid id) |
| `GET /api/surveys/:id` | yes | Survey detail (owner only; 404 otherwise) |
| `PUT /api/surveys/:id/files/*path` | yes | Stream body into R2 at `users/<uid>/<survey>/raw/<path>`; 409 while building |
| `POST /api/surveys/:id/build` | yes | Mark `building`, POST to Modal (`Modal-Key`/`Modal-Secret` headers) with `survey_id`, `prefix`, `callback_url` |
| `GET /api/surveys/:id/model.glb` | yes | Stream `users/<uid>/<survey>/model.glb` from R2 once `ready` |
| `POST /api/internal/surveys/:id/status` | `x-callback-secret` header | Modal callback setting `ready`/`failed` |

Auth: `Authorization: Bearer <token>` or the `session` cookie. Sessions last 30 days (`SESSION_TTL`).

### Survey lifecycle

`uploading -> building -> ready | failed`. Modal never writes to D1; it calls the callback endpoint. `finish_build` only updates rows still in `building`. Builds that never call back are marked `failed` ("Build timed out") lazily in `SurveyRepository.list_for_user` after `STALE_BUILD_SECONDS` (2h), so they can be retried.

### Frontend (`public/`)

Plain HTML, no build step. `index.html` handles signup/login and survey list via `/api/...`; `viewer.html?id=<survey>` loads the GLB with three.js 0.170 from jsDelivr (import map).

## Gotchas

- **Pyodide/JS boundary**: JS `null` may be `None` or `jsnull` depending on Pyodide version; use `is_null()` from `store.py` (e.g. for R2 `get` results). Convert JS results with `to_py()` (JSON round trip; passes through values the Workers SDK already converted to Python, which it does for D1 results under `pywrangler`). Use `js_object()` when passing dicts to JS APIs (e.g. R2 `put` options).
- **Never bind `None` to D1**: Pyodide passes it as `undefined`, which D1 rejects. Use `NULLIF(?, '')` with an empty string, as in `finish_build`.
- R2 prefix layout is built only by `Services.prefix(user_id, survey_id)`; every survey route goes through the `owned_survey` dependency (`Services.owned_survey`) for ownership checks. Keep new routes consistent with both.
- Uploads stream `request.body` directly to R2 (see `uploads.py`); request bodies are capped at 100 MB (Free/Pro), so clients must upload files individually. Any other route that must accept large bodies needs the same bypass. The model download streams the R2 body through a `StreamingResponse`.
- New routes: add to `app.py` and depend on `current_user` (or `owned_survey`) unless intentionally public.
- The JS `Request` is not available inside FastAPI handlers; use `request.scope["env"]` (via `get_services`) for bindings and secrets.
