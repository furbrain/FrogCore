"""FrogCore HTTP API (FastAPI).

The streaming upload route is not here: the Workers ASGI adapter buffers whole request bodies, so
`PUT /api/surveys/:id/files/*path` is handled by `uploads.py` before FastAPI sees the request.
"""
import base64
import binascii
import json
import re
import uuid
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from workers import fetch

from security import Tokens
from services import Services, get_services
from store import SESSION_TTL, is_null

USERNAME_RE = re.compile(r"[a-z0-9_-]{3,32}", re.IGNORECASE)
MIN_PASSWORD_LENGTH = 10

app = FastAPI(title="FrogCore", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")


# ---------- errors: every failure is {"error": message}, which the frontend and Cyclops rely on ----------
@app.exception_handler(StarletteHTTPException)  # also covers the router's own 404 / 405
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"error": "Invalid request body"}, status_code=400)


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    print(f"unhandled error: {exc!r}")  # never leak internals to clients
    return JSONResponse({"error": "internal error"}, status_code=500)


# ---------- dependencies ----------
async def current_user(
    svc: Services = Depends(get_services),
    authorization: str | None = Header(None),
    cookie: str | None = Header(None),
) -> int:
    return await svc.authenticate(authorization, cookie)


async def owned_survey(
    survey_id: str, user_id: int = Depends(current_user), svc: Services = Depends(get_services)
) -> dict:
    return await svc.owned_survey(survey_id, user_id)


def callback_authorized(
    svc: Services = Depends(get_services), x_callback_secret: str | None = Header(None)
) -> None:
    if not Tokens.equal(x_callback_secret or "", svc.env.CALLBACK_SECRET):
        raise HTTPException(403, "Forbidden")


# ---------- auth ----------
class Credentials(BaseModel):
    username: str = ""
    password: str = ""


async def start_session(svc: Services, response: Response, user_id: int) -> dict:
    token = await svc.sessions.create(user_id)
    response.set_cookie("session", token, max_age=SESSION_TTL, path="/",
                        httponly=True, secure=True, samesite="lax")
    return {"token": token}


@app.post("/api/signup")
async def signup(body: Credentials, response: Response, svc: Services = Depends(get_services)) -> dict:
    if not USERNAME_RE.fullmatch(body.username) or len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, "Username: 3-32 characters (a-z, 0-9, _ or -). "
                                 f"Password: at least {MIN_PASSWORD_LENGTH} characters.")
    if await svc.users.find_by_name(body.username) is not None:
        raise HTTPException(409, "That username is taken")
    pw_hash = await svc.hasher.hash(body.password)
    try:
        user_id = await svc.users.create(body.username, pw_hash)
    except Exception:  # unique constraint (two signups racing)
        raise HTTPException(409, "That username is taken") from None
    return await start_session(svc, response, user_id)


@app.post("/api/login")
async def login(
    response: Response,
    body: Credentials | None = None,
    authorization: str | None = Header(None),
    svc: Services = Depends(get_services),
) -> dict:
    """Accepts JSON {username, password} (browser) or HTTP Basic auth (Cyclops)."""
    if authorization and authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[len("Basic "):]).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise HTTPException(400, "Malformed Basic credentials") from None
        username, _, password = decoded.partition(":")
    elif body is not None:
        username, password = body.username, body.password
    else:
        raise HTTPException(400, "Request body must be JSON")
    user = await svc.users.find_by_name(username)
    if user is None or not await svc.hasher.verify(password, user["pw_hash"]):
        raise HTTPException(401, "Wrong username or password")
    return await start_session(svc, response, int(user["id"]))


@app.post("/api/logout")
async def logout(
    response: Response,
    _: int = Depends(current_user),
    svc: Services = Depends(get_services),
    authorization: str | None = Header(None),
    cookie: str | None = Header(None),
) -> dict:
    token = svc.token(authorization, cookie)
    if token:
        await svc.sessions.delete(token)
    response.delete_cookie("session", path="/", httponly=True, secure=True, samesite="lax")
    return {"ok": True}


# ---------- surveys ----------
class NewSurvey(BaseModel):
    name: str | None = None


class BuildStatus(BaseModel):
    status: Literal["ready", "failed"]
    error: str | None = None


@app.get("/api/surveys")
async def list_surveys(user_id: int = Depends(current_user), svc: Services = Depends(get_services)) -> list[dict]:
    return await svc.surveys.list_for_user(user_id)


@app.post("/api/surveys", status_code=201)
async def create_survey(
    body: NewSurvey, user_id: int = Depends(current_user), svc: Services = Depends(get_services)
) -> dict:
    survey_id = str(uuid.uuid4())
    await svc.surveys.create(survey_id, user_id, (body.name or "Untitled")[:100])
    return {"id": survey_id}


@app.get("/api/surveys/{survey_id}")
async def get_survey(survey: dict = Depends(owned_survey)) -> dict:
    return survey


@app.post("/api/surveys/{survey_id}/build", status_code=202)
async def build(
    request: Request,
    survey: dict = Depends(owned_survey),
    user_id: int = Depends(current_user),
    svc: Services = Depends(get_services),
) -> dict:
    if survey["status"] == "building":
        raise HTTPException(409, "Already building")
    await svc.surveys.mark_building(survey["id"])
    origin = str(request.base_url).rstrip("/")
    payload = {
        "survey_id": survey["id"],
        "prefix": svc.prefix(user_id, survey["id"]),
        "callback_url": f"{origin}/api/internal/surveys/{survey['id']}/status",
    }
    try:
        resp = await fetch(
            svc.env.MODAL_URL,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Modal-Key": svc.env.MODAL_KEY,
                "Modal-Secret": svc.env.MODAL_SECRET,
            },
            body=json.dumps(payload),
        )
        started = 200 <= resp.status < 300
    except Exception as e:
        print(f"modal trigger failed: {e!r}")
        started = False
    if not started:
        await svc.surveys.mark_failed(survey["id"], "Could not start build")
        raise HTTPException(502, "Could not start build")
    return {"status": "building"}


@app.post("/api/internal/surveys/{survey_id}/status", dependencies=[Depends(callback_authorized)])
async def build_finished(survey_id: str, body: BuildStatus, svc: Services = Depends(get_services)) -> dict:
    """Called by Modal when a build ends."""
    await svc.surveys.finish_build(survey_id, body.status, (body.error or "")[:500])
    return {"ok": True}


@app.get("/api/surveys/{survey_id}/model.glb")
async def download_model(
    survey: dict = Depends(owned_survey),
    user_id: int = Depends(current_user),
    svc: Services = Depends(get_services),
) -> StreamingResponse:
    if survey["status"] != "ready":
        raise HTTPException(404, "Model not ready")
    obj = await svc.bucket.get(f"{svc.prefix(user_id, survey['id'])}model.glb")
    if is_null(obj):
        raise HTTPException(404, "Model not found")

    async def chunks():
        async for chunk in obj.body:  # JS ReadableStream -> bytes, without buffering the whole model
            yield chunk.to_bytes()

    return StreamingResponse(chunks(), media_type="model/gltf-binary", headers={
        "ETag": obj.httpEtag,
        "Cache-Control": "private, max-age=3600",
    })
