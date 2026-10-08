"""Optional shared-password gate, used when the app is reachable from other devices on the home network.

The password is stored only as a salted PBKDF2 hash in data/access.json (set it with set_password.bat).
While that file exists, every request from another machine must present it via HTTP Basic auth
(any username, the shared password); requests from the host PC itself (localhost) are let through so
the person sitting at it isn't prompted. If the file is missing, nothing is enforced -- start.bat only
opens the server to the network when the file exists, so that state is localhost-only.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

ACCESS_FILE = Path(os.environ.get("BUDGET_ACCESS_FILE") or Path(__file__).resolve().parent.parent / "data" / "access.json")
ITERATIONS = 200_000
LOCAL_HOSTS = {"127.0.0.1", "::1"}


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCESS_FILE.write_text(json.dumps({
        "salt": salt.hex(),
        "iterations": ITERATIONS,
        "hash": _hash(password, salt).hex(),
    }), encoding="utf-8")


def _load():
    """None if no password has been set; otherwise the stored record. A file that exists but can't be
    read comes back as {} -- i.e. still "configured", so it fails closed rather than opening access up."""
    if not ACCESS_FILE.exists():
        return None
    try:
        return json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _password_ok(record: dict, password: str) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        return hmac.compare_digest(_hash(password, salt, int(record["iterations"])), expected)
    except (KeyError, ValueError, TypeError):
        return False


def _password_from_header(header: str):
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(token.strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded.partition(":")[2]


class PasswordGate:
    """Pure-ASGI middleware enforcing the shared password on non-local requests."""

    def __init__(self, app):
        self.app = app
        self._accepted: set[str] = set()   # Authorization headers already verified (PBKDF2 is slow)
        self._accepted_mtime = None

    def _is_authorized(self, record: dict, header: str) -> bool:
        mtime = ACCESS_FILE.stat().st_mtime if ACCESS_FILE.exists() else None
        if mtime != self._accepted_mtime:
            self._accepted.clear()             # password changed: forget everything verified before
            self._accepted_mtime = mtime
        if header in self._accepted:
            return True
        password = _password_from_header(header)
        if password is not None and _password_ok(record, password):
            self._accepted.add(header)
            return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        record = _load()
        client_host = (scope.get("client") or ("",))[0]
        if record is None or client_host in LOCAL_HOSTS:
            return await self.app(scope, receive, send)

        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        if self._is_authorized(record, header):
            return await self.app(scope, receive, send)

        await asyncio.sleep(1)                 # slows down password guessing
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [
                (b"www-authenticate", b'Basic realm="Budget Tracker", charset="UTF-8"'),
                (b"content-type", b"text/plain; charset=utf-8"),
            ],
        })
        await send({"type": "http.response.body", "body": b"Password required."})
