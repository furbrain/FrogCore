"""Streaming file upload, handled outside FastAPI.

The Workers ASGI adapter reads the entire request body into memory before calling the app. Uploads
can be up to 100 MB, so this route works on the raw Worker request and streams its body to R2.
"""
import re
from urllib.parse import unquote, urlparse

from fastapi import HTTPException
from workers import Response

from security import js_object
from services import Services

UPLOAD_PATH = re.compile(r"/api/surveys/([^/]+)/files/(.+)")


def is_upload(request) -> bool:
    return request.method == "PUT" and UPLOAD_PATH.fullmatch(urlparse(request.url).path) is not None


async def handle_upload(request, env) -> Response:
    try:
        return await _upload(request, Services(env))
    except HTTPException as e:
        return Response.json({"error": e.detail}, status=e.status_code)
    except Exception as e:  # never leak internals to clients
        print(f"unhandled error: {e!r}")
        return Response.json({"error": "internal error"}, status=500)


async def _upload(request, svc: Services) -> Response:
    user_id = await svc.authenticate(request.headers.get("Authorization"), request.headers.get("Cookie"))
    survey_id, rel = (unquote(part) for part in UPLOAD_PATH.fullmatch(urlparse(request.url).path).groups())
    survey = await svc.owned_survey(survey_id, user_id)
    if survey["status"] == "building":
        raise HTTPException(409, "A build is in progress")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise HTTPException(400, "Bad file path")
    options = js_object({"httpMetadata": {
        "contentType": request.headers.get("Content-Type") or "application/octet-stream"}})
    await svc.bucket.put(f"{svc.prefix(user_id, survey['id'])}raw/{rel}", request.body, options)
    return Response.json({"ok": True})
