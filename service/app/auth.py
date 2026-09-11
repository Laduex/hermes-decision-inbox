"""Trusted-proxy identity validation and short-lived browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from .database import Database, iso, now


class AuthenticationError(RuntimeError):
    pass


def validate_tailscale_identity(login: str, owner_login: str) -> str:
    """Accept only the owner identity injected by a localhost Tailscale Serve proxy."""
    normalized = login.strip().lower()
    expected = owner_login.strip().lower()
    if not normalized or not expected:
        raise AuthenticationError("Tailscale authentication is not configured")
    if not hmac.compare_digest(normalized, expected):
        raise AuthenticationError("Tailscale identity is not authorized")
    return normalized


def validate_telegram_init_data(init_data: str, bot_token: str, owner_id: int,
                                max_age_seconds: int = 300) -> dict:
    if not bot_token or not owner_id:
        raise AuthenticationError("Telegram authentication is not configured")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    supplied_hash = pairs.pop("hash", "")
    if not supplied_hash:
        raise AuthenticationError("missing Telegram signature")
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise AuthenticationError("invalid Telegram signature")
    try:
        auth_date = int(pairs["auth_date"])
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("invalid Telegram identity") from exc
    current = int(time.time())
    if auth_date > current + 30 or current - auth_date > max_age_seconds:
        raise AuthenticationError("expired Telegram authentication")
    if user_id != owner_id:
        raise AuthenticationError("Telegram identity is not authorized")
    nonce = pairs.get("query_id") or supplied_hash
    return {"user_id": user_id, "nonce": nonce, "auth_date": auth_date}


def record_nonce(db: Database, nonce: str, user_id: int, ttl_seconds: int = 300) -> None:
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    expires = now().timestamp() + ttl_seconds
    with db.transaction() as conn:
        conn.execute("DELETE FROM auth_nonces WHERE expires_at<?", (iso(now()),))
        try:
            conn.execute(
                "INSERT INTO auth_nonces VALUES(?,?,?,?)",
                (digest, user_id, iso(datetime.fromtimestamp(expires, tz=timezone.utc)), iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise AuthenticationError("Telegram authentication was already used") from exc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def issue_session(user_id: int, signing_key: str, ttl_seconds: int = 900) -> str:
    payload = {
        "uid": user_id,
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_urlsafe(18),
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64(hmac.new(signing_key.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def validate_session(token: str, signing_key: str, owner_id: int) -> int:
    try:
        encoded, supplied = token.split(".", 1)
        expected = _b64(hmac.new(signing_key.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, supplied):
            raise AuthenticationError("invalid session")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if int(payload["exp"]) < int(time.time()) or int(payload["uid"]) != owner_id:
            raise AuthenticationError("expired session")
        return int(payload["uid"])
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("invalid session") from exc


@dataclass
class RateLimiter:
    limit: int = 30
    window_seconds: int = 60

    def __post_init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        current = time.monotonic()
        cutoff = current - self.window_seconds
        hits = [stamp for stamp in self._hits.get(key, []) if stamp >= cutoff]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(current)
        self._hits[key] = hits
        return True
