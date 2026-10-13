"""Per-request services shared by the FastAPI routes and the raw upload handler."""
from http.cookies import CookieError, SimpleCookie
from typing import Any

from fastapi import HTTPException, Request

from security import PasswordHasher, Tokens
from store import Database, SessionRepository, SurveyRepository, UserRepository


class Services:
    """Bindings and repositories, built fresh for every request from the Worker `env`."""

    def __init__(self, env: Any):
        self.env = env
        self.bucket = env.footage_bucket
        db = Database(env.DB)
        self.users = UserRepository(db)
        self.sessions = SessionRepository(db, Tokens)
        self.surveys = SurveyRepository(db)
        self.hasher = PasswordHasher()

    # ---------- auth ----------
    @staticmethod
    def token(authorization: str | None, cookie: str | None) -> str | None:
        """The session token from `Authorization: Bearer <token>` or the `session` cookie."""
        if authorization and authorization.startswith("Bearer "):
            return authorization[len("Bearer "):]
        if cookie:
            jar = SimpleCookie()
            try:
                jar.load(cookie)
            except CookieError:
                return None
            if "session" in jar:
                return jar["session"].value
        return None

    async def authenticate(self, authorization: str | None, cookie: str | None) -> int:
        token = self.token(authorization, cookie)
        user_id = await self.sessions.user_id_for(token) if token else None
        if user_id is None:
            raise HTTPException(401, "Please log in")
        return user_id

    async def owned_survey(self, survey_id: str, user_id: int) -> dict:
        survey = await self.surveys.get_owned(survey_id, user_id)
        if survey is None:
            raise HTTPException(404, "Survey not found")
        return survey

    # ---------- R2 layout ----------
    @staticmethod
    def prefix(user_id: int, survey_id: str) -> str:
        return f"users/{user_id}/{survey_id}/"


def get_services(request: Request) -> Services:
    """FastAPI dependency; the Workers ASGI adapter puts the Worker `env` in the ASGI scope."""
    return Services(request.scope["env"])
