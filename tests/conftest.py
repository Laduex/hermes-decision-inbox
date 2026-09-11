from __future__ import annotations

from pathlib import Path

import pytest

from service.app.config import Settings
from service.app.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "state" / "inbox.sqlite3",
        signing_key="test-signing-key-that-is-long-enough",
        publish_tokens={"iris": "iris-token", "default": "default-token"},
        telegram_bot_token="123456:TEST_TOKEN",
        telegram_owner_id=424242,
        telegram_chat_id="424242",
        tailscale_owner_login="owner@example.com",
        mini_app_url="https://test.example/inbox",
        hermes_api_url="http://127.0.0.1:18642",
        hermes_profile_api_keys={"iris": "hermes-iris", "default": "hermes-default"},
        manifest_root=tmp_path / "state" / "manifests",
        wiki_root=tmp_path / "wiki",
        backup_root=tmp_path / "backups",
        worker_enabled=False,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path, settings.manifest_root)
    database.initialize()
    return database


@pytest.fixture
def single_request() -> dict:
    return {
        "name": "deploy-choice",
        "title": "Choose the deployment strategy",
        "summary": "A decision is required before deployment can continue.",
        "details": "Staged rollout is reversible; direct rollout is faster but riskier.",
        "recommendation": {
            "option_id": "staged", "label": "Use staged deployment",
            "reason": "It provides a reversible production canary.",
        },
        "alternatives": [{
            "option_id": "direct", "label": "Deploy directly",
            "details": "Faster, with greater production risk.",
        }],
        "evidence": [],
        "priority": "normal",
        "source_profile": "iris",
        "source_session_id": "session-1",
        "source_task_id": "task-1",
        "plugin_version": "0.1.0",
    }
