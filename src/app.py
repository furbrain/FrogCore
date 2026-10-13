"""FrogCore HTTP API."""
import base64
import binascii
import json
import re
import uuid
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlparse

from workers import Response, fetch

from router import ApiRequest, HttpError, Router, json_response
from security import PasswordHasher, Tokens, js_object
from store import SESSION_TTL, Database, SessionRepository, SurveyRepository, UserRepository, is_null

USERNAME_RE = re.compile(r"[a-z0-9_-]{3,32}", re.IGNORECASE)
MIN_PASSWORD_LENGTH = 10


class FrogCoreApp:
    def __init__(self, env):
        self.env = env
        self.bucket = env.footage_bucket
        db = Database(env.DB)
        self.users = UserRepository(db)
        self.sessions = SessionRepository(db, Tokens)
        self.surveys = SurveyRepository(db)
        self.hasher = PasswordHasher()

        self.router = Router()
        add = self.router.add
        add("POST", "/api/signup", self.signup)
        add("POST", "/api/login", self.login)
        add("POST", "/api/logout", self.logout, auth=True)
        add("GET", "/api/surveys", self.list_surveys, auth=True)
        add("POST", "/api/surveys", self.create_survey, auth=True)
        add("GET", "/api/surveys/:id", self.get_survey, auth=True)
        add("PUT", "/api/surveys/:id/files/*path", self.upload_file, auth=True)
        add("POST", "/api/surveys/:id/build", self.build, auth=True)
        add("GET", "/api/surveys/:id/model.glb", self.download_model, auth=True)
        add("POST", "/api/internal/surveys/:id/status", self.build_finished)

    # ---------- dispatch ----------
    async def handle(self, raw) -> Response:
        try:
            parsed = urlparse(raw.url)
            route, params = self.router.resolve(raw.method, parsed.path)
            req = ApiRequest(raw, params, origin=f"{parsed.scheme}://{parsed.netloc}")
            if route.auth:
                req.user_id = await self._authenticate(req)
            return await route.handler(req)
        except HttpError as e:
            return json_response({"error": e.message}, e.status)
        except Exception as e:  # never leak internals to clients
            print(f"unhandled error: {e!r}")
            return json_response({"error": "internal error"}, 500)

    # ---------- auth helpers ----------
    @staticmethod
    def _token(req: ApiRequest) -> str | None:
        auth = req.header("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
        cookie_header = req.header("Cookie")
        if cookie_header:
            jar = SimpleCookie()
            try:
                jar.load(cookie_header)
            except CookieError:
                return None
            if "session" in jar:
                return jar["session"].value
        return None

    async def _authenticate(self, req: ApiRequest) -> int:
        token = self._token(req)
        user_id = await self.sessions.user_id_for(token) if token else None
        if user_id is None:
            raise HttpError(401, "Please log in")
        return user_id

    async def _start_session(self, user_id: int) -> Response:
        token = await self.sessions.create(user_id)
        cookie = f"session={token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}"
        return json_response({"token": token}, headers={"Set-Cookie": cookie})

    # ---------- auth routes ----------
    async def signup(self, req: ApiRequest) -> Response:
        body = await req.json()
        username, password = str(body.get("username", "")), str(body.get("password", ""))
        if not USERNAME_RE.fullmatch(username) or len(password) < MIN_PASSWORD_LENGTH:
            raise HttpError(400, "Username: 3-32 characters (a-z, 0-9, _ or -). "
                                 f"Password: at least {MIN_PASSWORD_LENGTH} characters.")
        if await self.users.find_by_name(username) is not None:
            raise HttpError(409, "That username is taken")
        pw_hash = await self.hasher.hash(password)
        try:
            user_id = await self.users.create(username, pw_hash)
        except Exception:  # unique constraint (two signups racing)
            raise HttpError(409, "That username is taken") from None
        return await self._start_session(user_id)

    async def login(self, req: ApiRequest) -> Response:
        """Accepts JSON {username, password} (browser) or HTTP Basic auth (Cyclops)."""
        auth = req.header("Authorization") or ""
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[len("Basic "):]).decode()
            except (binascii.Error, UnicodeDecodeError):
                raise HttpError(400, "Malformed Basic credentials") from None
            username, _, password = decoded.partition(":")
        else:
            body = await req.json()
            username, password = str(body.get("username", "")), str(body.get("password", ""))
        user = await self.users.find_by_name(username)
        if user is None or not await self.hasher.verify(password, user["pw_hash"]):
            raise HttpError(401, "Wrong username or password")
        return await self._start_session(int(user["id"]))

    async def logout(self, req: ApiRequest) -> Response:
        token = self._token(req)
        if token:
            await self.sessions.delete(token)
        return json_response({"ok": True}, headers={
            "Set-Cookie": "session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"})

    # ---------- surveys ----------
    @staticmethod
    def _prefix(user_id: int, survey_id: str) -> str:
        return f"users/{user_id}/{survey_id}/"

    async def _owned(self, req: ApiRequest) -> dict:
        survey = await self.surveys.get_owned(req.params["id"], req.user_id)
        if survey is None:
            raise HttpError(404, "Survey not found")
        return survey

    async def list_surveys(self, req: ApiRequest) -> Response:
        return json_response(await self.surveys.list_for_user(req.user_id))

    async def create_survey(self, req: ApiRequest) -> Response:
        body = await req.json()
        name = str(body.get("name") or "Untitled")[:100]
        survey_id = str(uuid.uuid4())
        await self.surveys.create(survey_id, req.user_id, name)
        return json_response({"id": survey_id}, 201)

    async def get_survey(self, req: ApiRequest) -> Response:
        return json_response(await self._owned(req))

    async def upload_file(self, req: ApiRequest) -> Response:
        """Streams the request body straight into R2 (request bodies are capped at 100 MB on Free/Pro)."""
        survey = await self._owned(req)
        if survey["status"] == "building":
            raise HttpError(409, "A build is in progress")
        rel = req.params["path"]
        if any(part in ("", ".", "..") for part in rel.split("/")):
            raise HttpError(400, "Bad file path")
        options = js_object({"httpMetadata": {
            "contentType": req.header("Content-Type") or "application/octet-stream"}})
        key = f"{self._prefix(req.user_id, survey['id'])}raw/{rel}"
        await self.bucket.put(key, req.raw.body, options)
        return json_response({"ok": True})

    async def build(self, req: ApiRequest) -> Response:
        survey = await self._owned(req)
        if survey["status"] == "building":
            raise HttpError(409, "Already building")
        await self.surveys.mark_building(survey["id"])
        payload = {
            "survey_id": survey["id"],
            "prefix": self._prefix(req.user_id, survey["id"]),
            "callback_url": f"{req.origin}/api/internal/surveys/{survey['id']}/status",
        }
        try:
            resp = await fetch(
                self.env.MODAL_URL,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Modal-Key": self.env.MODAL_KEY,
                    "Modal-Secret": self.env.MODAL_SECRET,
                },
                body=json.dumps(payload),
            )
            started = 200 <= resp.status < 300
        except Exception as e:
            print(f"modal trigger failed: {e!r}")
            started = False
        if not started:
            await self.surveys.mark_failed(survey["id"], "Could not start build")
            raise HttpError(502, "Could not start build")
        return json_response({"status": "building"}, 202)

    async def build_finished(self, req: ApiRequest) -> Response:
        """Called by Modal when a build ends."""
        if not Tokens.equal(req.header("x-callback-secret") or "", self.env.CALLBACK_SECRET):
            raise HttpError(403, "Forbidden")
        body = await req.json()
        status = body.get("status")
        if status not in ("ready", "failed"):
            raise HttpError(400, "Bad status")
        await self.surveys.finish_build(req.params["id"], status, str(body.get("error") or "")[:500])
        return json_response({"ok": True})

    async def download_model(self, req: ApiRequest) -> Response:
        survey = await self._owned(req)
        if survey["status"] != "ready":
            raise HttpError(404, "Model not ready")
        obj = await self.bucket.get(f"{self._prefix(req.user_id, survey['id'])}model.glb")
        if is_null(obj):
            raise HttpError(404, "Model not found")
        return Response(obj.body, headers={
            "Content-Type": "model/gltf-binary",
            "Content-Length": str(obj.size),
            "ETag": obj.httpEtag,
            "Cache-Control": "private, max-age=3600",
        })
