"""Tiny path router and HTTP helpers."""
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

from workers import Response


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    return Response(json.dumps(data), status=status, headers=merged)


class ApiRequest:
    """A request plus the path parameters matched by the router."""

    def __init__(self, raw: Any, params: dict[str, str], origin: str):
        self.raw = raw
        self.params = params
        self.origin = origin
        self.user_id: int | None = None

    def header(self, name: str) -> str | None:
        return self.raw.headers.get(name)

    async def json(self) -> dict[str, Any]:
        try:
            data = json.loads(await self.raw.text())
        except ValueError:
            raise HttpError(400, "Request body must be JSON") from None
        if not isinstance(data, dict):
            raise HttpError(400, "Request body must be a JSON object")
        return data


Handler = Callable[[ApiRequest], Awaitable[Response]]


@dataclass(frozen=True)
class Route:
    method: str
    segments: tuple[str, ...]
    handler: Handler
    auth: bool

    def match(self, parts: list[str]) -> dict[str, str] | None:
        """':name' matches one segment; '*name' matches the rest of the path (at least one segment)."""
        params: dict[str, str] = {}
        for i, seg in enumerate(self.segments):
            if seg.startswith("*"):
                rest = parts[i:]
                if not rest or not all(rest):
                    return None
                params[seg[1:]] = "/".join(rest)
                return params
            if i >= len(parts):
                return None
            if seg.startswith(":"):
                params[seg[1:]] = parts[i]
            elif seg != parts[i]:
                return None
        return params if len(parts) == len(self.segments) else None


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, *, auth: bool = False) -> None:
        segments = tuple(pattern.strip("/").split("/"))
        self._routes.append(Route(method, segments, handler, auth))

    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        parts = [unquote(p) for p in path.strip("/").split("/")]
        path_matched = False
        for route in self._routes:
            params = route.match(parts)
            if params is None:
                continue
            if route.method == method:
                return route, params
            path_matched = True
        raise HttpError(405 if path_matched else 404, "Method not allowed" if path_matched else "Not found")
