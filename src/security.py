"""Password hashing and session tokens."""
import hashlib
import hmac
import secrets

from js import Object, TextEncoder, Uint8Array, crypto
from pyodide.ffi import to_js


def js_object(value):
    """Convert a Python dict (recursively) to a plain JavaScript object."""
    return to_js(value, dict_converter=Object.fromEntries)


class PasswordHasher:
    """PBKDF2-SHA256 using the runtime's native WebCrypto (100k iterations is the Workers maximum)."""

    ITERATIONS = 100_000
    SALT_BYTES = 16

    async def _derive(self, password: str, salt: bytes) -> bytes:
        key = await crypto.subtle.importKey(
            "raw", TextEncoder.new().encode(password), "PBKDF2", False, to_js(["deriveBits"]))
        params = js_object({
            "name": "PBKDF2",
            "hash": "SHA-256",
            "salt": Uint8Array.new(to_js(list(salt))),
            "iterations": self.ITERATIONS,
        })
        bits = await crypto.subtle.deriveBits(params, key, 256)
        return Uint8Array.new(bits).to_bytes()

    async def hash(self, password: str) -> str:
        salt = secrets.token_bytes(self.SALT_BYTES)
        return f"{salt.hex()}${(await self._derive(password, salt)).hex()}"

    async def verify(self, password: str, stored: str) -> bool:
        salt_hex, _, digest_hex = stored.partition("$")
        derived = await self._derive(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(derived, bytes.fromhex(digest_hex))


class Tokens:
    @staticmethod
    def new() -> str:
        return secrets.token_hex(32)

    @staticmethod
    def digest(token: str) -> str:
        """Only the SHA-256 of a session token is stored, so a database leak can't be replayed."""
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def equal(a: str, b: str) -> bool:
        return hmac.compare_digest(a.encode(), b.encode())
