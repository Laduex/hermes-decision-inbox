from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
import pytest


def test_real_hermes_discovers_external_plugin(tmp_path, monkeypatch):
    pytest.importorskip("hermes_cli")
    from hermes_cli import plugins as plugins_mod
    from tools.registry import registry

    repo = Path(__file__).resolve().parents[1]
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "hermes-decision-inbox").symlink_to(repo, target_is_directory=True)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {
            "enabled": ["hermes-decision-inbox"],
            "entries": {"hermes-decision-inbox": {"settings": {"service_url": "http://127.0.0.1:8080"}}},
        }
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("DECISION_INBOX_PUBLISH_TOKEN", "test-token")
    manager = plugins_mod.PluginManager(scope_key=str(home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins.get("hermes-decision-inbox")
    assert loaded is not None and loaded.enabled and loaded.error is None
    entry = registry.get_entry("publish_weekly_wiki_review", scope=str(home.resolve()))
    assert entry is not None
    assert entry.toolset == "decision_inbox"
    assert manager.find_plugin_skill("hermes-decision-inbox:decision-inbox-routing") is not None


def weekly_args(single_request):
    card = {k: v for k, v in single_request.items() if not k.startswith("source_") and k not in {"plugin_version", "name"}}
    card["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
        "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Weekly memory",
    }
    return {"name": "weekly-memory-wiki-review", "batch": [card]}


def test_handler_captures_profile_session_and_returns_published(monkeypatch, single_request):
    from hermes_decision_inbox.tool import build_handler

    class Context:
        profile_name = "default"
        def get_config(self, key, default=None):
            if key == "service_url":
                return "http://decision.test"
            if key == "auto_resume":
                return False
            return default

    captured = {}
    def publish(self, payload):
        captured.update(payload)
        return {"decision_id": "dec_test", "deduplicated": False}

    monkeypatch.setenv("DECISION_INBOX_PUBLISH_TOKEN", "default-token")
    monkeypatch.setattr("hermes_decision_inbox.tool.DecisionInboxClient.publish", publish)
    args = weekly_args(single_request)
    result = json.loads(build_handler(Context())(args, session_id="session-1", task_id="task-1"))
    assert result["status"] == "PUBLISHED"
    assert captured["source_profile"] == "default"
    assert captured["source_session_id"] == "session-1"
    assert captured["auto_resume"] is False
    assert captured["default_expiry_days"] == 14


def test_handler_uses_multiplex_profile_secret_scope(monkeypatch, single_request):
    pytest.importorskip("agent.secret_scope")
    from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
    from hermes_decision_inbox.tool import build_handler

    class Context:
        profile_name = "default"
        def get_config(self, key, default=None):
            return "http://decision.test" if key == "service_url" else default

    seen = {}
    def publish(self, payload):
        seen["token"] = self.publish_token
        return {"decision_id": "dec_scoped"}

    monkeypatch.setattr("hermes_decision_inbox.tool.DecisionInboxClient.publish", publish)
    monkeypatch.setenv("DECISION_INBOX_PUBLISH_TOKEN", "wrong-global-token")
    set_multiplex_active(True)
    scope_token = set_secret_scope({"DECISION_INBOX_PUBLISH_TOKEN": "default-scoped-token"})
    args = weekly_args(single_request)
    try:
        result = json.loads(build_handler(Context())(args, session_id="session-1", task_id="task-1"))
    finally:
        reset_secret_scope(scope_token)
        set_multiplex_active(False)
    assert result["status"] == "PUBLISHED"
    assert seen["token"] == "default-scoped-token"


def test_delegated_session_is_refused(monkeypatch, single_request):
    from hermes_decision_inbox.tool import build_handler

    class Context:
        profile_name = "default"
        def get_config(self, key, default=None):
            return "http://decision.test" if key == "service_url" else default

    monkeypatch.setenv("DECISION_INBOX_PUBLISH_TOKEN", "default-token")
    monkeypatch.setattr("hermes_decision_inbox.tool._session_is_delegated", lambda _: True)
    args = weekly_args(single_request)
    result = json.loads(build_handler(Context())(args, session_id="child-session", task_id="child-session"))
    assert result["status"] == "ERROR"
    assert "parent agent" in result["error"]
