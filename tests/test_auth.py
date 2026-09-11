from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from service.app.auth import (
    AuthenticationError, issue_session, record_nonce, validate_session, validate_telegram_init_data,
)


def signed_init_data(bot_token: str, user_id: int, *, query_id: str = "query-1", age: int = 0) -> str:
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": query_id,
        "user": json.dumps({"id": user_id, "first_name": "Vaughn"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_init_data_and_short_lived_session(settings):
    init_data = signed_init_data(settings.telegram_bot_token, settings.telegram_owner_id)
    identity = validate_telegram_init_data(init_data, settings.telegram_bot_token, settings.telegram_owner_id)
    token = issue_session(identity["user_id"], settings.signing_key)
    assert validate_session(token, settings.signing_key, settings.telegram_owner_id) == settings.telegram_owner_id


def test_wrong_owner_and_expired_data_are_rejected(settings):
    with pytest.raises(AuthenticationError):
        validate_telegram_init_data(
            signed_init_data(settings.telegram_bot_token, 7), settings.telegram_bot_token,
            settings.telegram_owner_id,
        )
    with pytest.raises(AuthenticationError):
        validate_telegram_init_data(
            signed_init_data(settings.telegram_bot_token, settings.telegram_owner_id, age=301),
            settings.telegram_bot_token, settings.telegram_owner_id,
        )


def test_nonce_replay_is_rejected(db):
    record_nonce(db, "same-query", 424242)
    with pytest.raises(AuthenticationError):
        record_nonce(db, "same-query", 424242)

