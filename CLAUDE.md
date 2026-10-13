# CLAUDE.md

FrogCore is a Cloudflare Python Worker (Pyodide) that backs a photogrammetry-style pipeline: users sign up, upload footage ("surveys") to R2, trigger a Modal build job that produces a `model.glb`, and view it in a three.js page.

`client/` (Cyclops uploader, `frogcore_client.py`) and `modal/` (build job, `build.py`) are separate components and are not documented here; this file covers the Worker, schema and static frontend.

## Commands

No package manager, no third-party Python deps, no test suite or linter configured.

- Deploy: `npx wrangler deploy`
- Local dev: `npx wrangler dev`
- Apply schema locally: `npx wrangler d1 execute frogcore --local --file=schema.sql` (schema is already applied in production; there is no migration tooling, so schema changes need manual `d1 execute`)
- Worker secrets (`wrangler secret put`): `MODAL_URL`, `MODAL_KEY`, `MODAL_SECRET`, `CALLBACK_SECRET`

## Architecture

Config: [wrangler.jsonc](wrangler.jsonc). Entry `src/entry.py`, `python_workers` compat flag, bindings `DB` (D1 `frogcore`) and `footage_bucket` (R2 bucket `footage`). Static assets are served from `public/`; `run_worker_first: ["/api/*"]` means only `/api/*` hits the Worker.

Python source in `src/` (flat imports, e.g. `from router import ...`):

- [entry.py](src/entry.py): `Default(WorkerEntrypoint)`; builds a fresh `FrogCoreApp(self.env)` per request.
- [app.py](src/app.py): `FrogCoreApp` registers all routes in `__init__` and holds the handlers. `handle()` resolves the route, enforces auth, and maps `HttpError` to JSON errors; any other exception becomes a generic 500 (logged, never leaked).
- [router.py](src/router.py): minimal router. `:name` matches one segment, `*name` matches the rest of the path; 404 vs 405 distinguished. Also `HttpError`, `json_response`, `ApiRequest` (wraps the raw request, `.json()` requires a JSON object).
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

- **Pyodide/JS boundary**: JS `null` may be `None` or `jsnull` depending on Pyodide version; use `is_null()` from `store.py` (e.g. for R2 `get` results). Convert JS results with `to_py()` (JSON round trip). Use `js_object()` when passing dicts to JS APIs (e.g. R2 `put` options).
- **Never bind `None` to D1**: Pyodide passes it as `undefined`, which D1 rejects. Use `NULLIF(?, '')` with an empty string, as in `finish_build`.
- R2 prefix layout is built only by `FrogCoreApp._prefix(user_id, survey_id)`; every survey route goes through `_owned()` for ownership checks. Keep new routes consistent with both.
- Uploads stream `req.raw.body` directly to R2; request bodies are capped at 100 MB (Free/Pro), so clients must upload files individually.
- Keep the Worker dependency-free (stdlib + `workers`/`js`/`pyodide` only) so plain `wrangler deploy` works.
- New routes: add in `FrogCoreApp.__init__` with `auth=True` unless intentionally public.
