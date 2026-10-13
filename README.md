# FrogCore

Python Worker (Cloudflare) + D1 + R2 (`footage_bucket` binding -> bucket `footage`) with a Modal build job.

- Users sign up at `/`, then use the same credentials from Cyclops (`client/frogcore_client.py`).
- Uploads go to `users/<uid>/<survey>/raw/...`; Modal writes `users/<uid>/<survey>/model.glb`.
- `/viewer.html?id=<survey>` shows the GLB with three.js (browser JavaScript).

## Layout
`src/entry.py` -> `app.py` (routes), `router.py`, `security.py`, `store.py` (D1 repositories).
No third-party Python packages, so `npx wrangler deploy` is enough.

## Worker secrets
`MODAL_URL`, `MODAL_KEY`, `MODAL_SECRET`, `CALLBACK_SECRET`.

## Modal
Create secret `frogcore-r2` (see `modal/build.py`), then `modal deploy modal/build.py`.
