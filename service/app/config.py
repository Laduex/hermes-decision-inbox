"""Environment-backed service settings; behavior defaults stay explicit."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: Path
    signing_key: str
    publish_tokens: dict[str, str]
    telegram_bot_token: str
    telegram_owner_id: int
    telegram_chat_id: str
    tailscale_owner_login: str
    mini_app_url: str
    hermes_api_url: str
    hermes_profile_api_keys: dict[str, str]
    manifest_root: Path
    wiki_root: Path
    backup_root: Path
    default_expiry_days: int = 14
    auto_resume: bool = True
    wiki_executor_enabled: bool = True
    worker_enabled: bool = True
    weekly_only: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        state_root = Path(os.environ.get("DECISION_INBOX_STATE_DIR", "./.local/state")).expanduser().resolve()
        raw_tokens = os.environ.get("DECISION_INBOX_PUBLISH_TOKENS", "")
        tokens = json.loads(raw_tokens) if raw_tokens else {}
        legacy = os.environ.get("DECISION_INBOX_PUBLISH_TOKEN", "").strip()
        if legacy and not tokens:
            tokens = {"*": legacy}
        raw_keys = os.environ.get("HERMES_PROFILE_API_KEYS", "")
        keys = json.loads(raw_keys) if raw_keys else {}
        return cls(
            database_path=Path(os.environ.get("DECISION_INBOX_DATABASE", state_root / "decision-inbox.sqlite3")),
            signing_key=os.environ.get("DECISION_INBOX_SIGNING_KEY", "local-development-only"),
            publish_tokens={str(k): str(v) for k, v in tokens.items()},
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_owner_id=int(os.environ.get("TELEGRAM_OWNER_ID", "0") or 0),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_OWNER_ID", "")),
            tailscale_owner_login=os.environ.get("DECISION_INBOX_TAILSCALE_OWNER_LOGIN", "").strip().lower(),
            mini_app_url=os.environ.get("DECISION_INBOX_MINI_APP_URL", "http://127.0.0.1:5173"),
            hermes_api_url=os.environ.get("DECISION_INBOX_HERMES_API_URL", "http://127.0.0.1:8642"),
            hermes_profile_api_keys={str(k): str(v) for k, v in keys.items()},
            manifest_root=Path(os.environ.get("DECISION_INBOX_MANIFEST_ROOT", state_root / "manifests")),
            wiki_root=Path(os.environ.get("DECISION_INBOX_WIKI_ROOT", state_root / "wiki")),
            backup_root=Path(os.environ.get("DECISION_INBOX_BACKUP_ROOT", state_root / "backups")),
            default_expiry_days=int(os.environ.get("DECISION_INBOX_DEFAULT_EXPIRY_DAYS", "14")),
            auto_resume=_bool("DECISION_INBOX_AUTO_RESUME", True),
            wiki_executor_enabled=_bool("DECISION_INBOX_WIKI_EXECUTOR_ENABLED", True),
            worker_enabled=_bool("DECISION_INBOX_WORKER_ENABLED", True),
            weekly_only=_bool("DECISION_INBOX_WEEKLY_ONLY", False),
        )
