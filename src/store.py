"""D1 access: a thin Database wrapper and one repository class per table."""
import json
import time
from typing import Any

from js import JSON

try:  # newer Pyodide represents JS null as `jsnull` rather than None
    from pyodide.ffi import jsnull
except ImportError:
    jsnull = None

SESSION_TTL = 30 * 86400
STALE_BUILD_SECONDS = 2 * 3600


def now() -> int:
    return int(time.time())


def is_null(value: Any) -> bool:
    return value is None or (jsnull is not None and value is jsnull)


def to_py(value: Any) -> Any:
    """Convert a JS value to plain Python via JSON (robust across Pyodide versions)."""
    return json.loads(JSON.stringify(value))


class Database:
    def __init__(self, d1: Any):
        self._d1 = d1

    def _statement(self, sql: str, params: tuple) -> Any:
        return self._d1.prepare(sql).bind(*params)

    async def first(self, sql: str, *params: Any) -> dict | None:
        row = await self._statement(sql, params).first()
        return None if is_null(row) else to_py(row)

    async def all(self, sql: str, *params: Any) -> list[dict]:
        result = await self._statement(sql, params).all()
        return to_py(result.results)

    async def run(self, sql: str, *params: Any) -> int:
        """Execute a statement and return D1's last_row_id."""
        result = await self._statement(sql, params).run()
        return int(result.meta.last_row_id)


class UserRepository:
    def __init__(self, db: Database):
        self._db = db

    async def find_by_name(self, username: str) -> dict | None:
        return await self._db.first("SELECT id, pw_hash FROM users WHERE username=?", username)

    async def create(self, username: str, pw_hash: str) -> int:
        return await self._db.run(
            "INSERT INTO users (username, pw_hash, created_at) VALUES (?,?,?)", username, pw_hash, now())


class SessionRepository:
    def __init__(self, db: Database, tokens: Any):
        self._db = db
        self._tokens = tokens

    async def create(self, user_id: int) -> str:
        token = self._tokens.new()
        await self._db.run(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
            self._tokens.digest(token), user_id, now() + SESSION_TTL)
        return token

    async def user_id_for(self, token: str) -> int | None:
        row = await self._db.first(
            "SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?",
            self._tokens.digest(token), now())
        return None if row is None else int(row["user_id"])

    async def delete(self, token: str) -> None:
        await self._db.run("DELETE FROM sessions WHERE token_hash=?", self._tokens.digest(token))


class SurveyRepository:
    def __init__(self, db: Database):
        self._db = db

    async def create(self, survey_id: str, user_id: int, name: str) -> None:
        await self._db.run(
            "INSERT INTO surveys (id, user_id, name, created_at) VALUES (?,?,?,?)",
            survey_id, user_id, name, now())

    async def get_owned(self, survey_id: str, user_id: int) -> dict | None:
        return await self._db.first(
            "SELECT id, name, status, error, created_at FROM surveys WHERE id=? AND user_id=?",
            survey_id, user_id)

    async def list_for_user(self, user_id: int) -> list[dict]:
        # Builds that never called back (Modal timeout/crash) are marked failed so they can be retried.
        await self._db.run(
            "UPDATE surveys SET status='failed', error='Build timed out' "
            "WHERE user_id=? AND status='building' AND building_since < ?",
            user_id, now() - STALE_BUILD_SECONDS)
        return await self._db.all(
            "SELECT id, name, status, error, created_at FROM surveys WHERE user_id=? ORDER BY created_at DESC",
            user_id)

    async def mark_building(self, survey_id: str) -> None:
        await self._db.run(
            "UPDATE surveys SET status='building', error=NULL, building_since=? WHERE id=?", now(), survey_id)

    async def mark_failed(self, survey_id: str, message: str) -> None:
        await self._db.run("UPDATE surveys SET status='failed', error=? WHERE id=?", message, survey_id)

    async def finish_build(self, survey_id: str, status: str, error: str) -> None:
        # NULLIF avoids binding None (Pyodide would pass it to D1 as undefined, which D1 rejects)
        await self._db.run(
            "UPDATE surveys SET status=?, error=NULLIF(?, '') WHERE id=? AND status='building'",
            status, error, survey_id)
