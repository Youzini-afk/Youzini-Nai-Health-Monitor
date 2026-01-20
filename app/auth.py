from __future__ import annotations

import base64
import hmac
import hashlib
import time
from dataclasses import dataclass

from fastapi import HTTPException

from app.config import settings


COOKIE_NAME = "nhm_session"


@dataclass(frozen=True)
class Session:
    username: str
    exp: int


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def _sign(payload: bytes) -> str:
    secret = (settings.auth_secret_key or "").encode("utf-8")
    if not secret:
        raise RuntimeError("AUTH_SECRET_KEY must be set when AUTH_ENABLED=true")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return _b64url_encode(sig)


def issue_session_cookie(username: str) -> str:
    exp = int(time.time()) + int(settings.auth_session_minutes or 1440) * 60
    payload = f"{username}|{exp}".encode("utf-8")
    token = f"{_b64url_encode(payload)}.{_sign(payload)}"
    return token


def verify_session_cookie(token: str | None) -> Session | None:
    if not token:
        return None
    try:
        payload_b64, sig = token.split(".", 1)
        payload = _b64url_decode(payload_b64)
        expected = _sign(payload)
        if not hmac.compare_digest(sig, expected):
            return None
        raw = payload.decode("utf-8", errors="replace")
        username, exp_s = raw.split("|", 1)
        exp = int(exp_s)
        if exp <= int(time.time()):
            return None
        return Session(username=username, exp=exp)
    except Exception:
        return None


def require_login(session: Session | None) -> Session:
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")
    if session.username != settings.auth_username:
        raise HTTPException(status_code=403, detail="Forbidden")
    return session


def verify_credentials(username: str, password: str) -> bool:
    if username != settings.auth_username:
        return False
    expected = settings.auth_password or ""
    if not expected:
        raise RuntimeError("AUTH_PASSWORD must be set when AUTH_ENABLED=true")
    return hmac.compare_digest(password, expected)

