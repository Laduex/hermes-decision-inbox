from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from hermes_decision_inbox.receiver import ContinuationReceiver, PublicationGuard


def test_publication_guard_replaces_interactive_silent_with_direct_link():
    guard = PublicationGuard()
    guard.record("session-1", {
        "decision_id": "dec_test",
        "decision_url": "https://inbox.test/?decision=dec_test",
    })
    result = guard.transform(response_text="[SILENT]", session_id="session-1")
    assert result == (
        "I need your decision: [Open Decision Inbox]"
        "(https://inbox.test/?decision=dec_test). I’ll continue here after you apply it."
    )
    assert guard.transform(response_text="unchanged", session_id="session-1") is None


def test_route_verification_accepts_compression_tip_but_rejects_reset(monkeypatch):
    rows = {
        "original": {"id": "original", "source": "telegram", "session_key": "route-1",
                     "ended_at": "2026-09-12T00:00:00+00:00", "end_reason": "compression"},
        "compressed": {"id": "compressed", "source": "telegram", "session_key": "route-1",
                       "ended_at": None, "end_reason": None},
    }

    class SessionDB:
        def __init__(self, read_only=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_session(self, session_id):
            return rows.get(session_id)

        def get_compression_tip(self, session_id):
            return "compressed" if session_id == "original" else session_id

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=SessionDB))
    ctx = SimpleNamespace(
        profile_name="iris",
        get_config=lambda key, default=None: default,
    )
    receiver = ContinuationReceiver(ctx)
    item = {
        "source_session_id": "original",
        "source_session_key": "route-1",
        "source_surface": "telegram",
    }
    assert receiver._route_target(item) == "compressed"

    rows["original"]["end_reason"] = "session_reset"
    rows["compressed"]["session_key"] = "route-2"
    assert receiver._route_target(item) is None


def test_route_verification_uses_real_sessiondb_compression_boundaries(tmp_path, monkeypatch):
    hermes_state = pytest.importorskip("hermes_state")
    session_db_type = hermes_state.SessionDB
    state_path = tmp_path / "hermes-state.sqlite3"
    route = "agent:iris:telegram:dm:424242"
    with session_db_type(db_path=state_path) as db:
        db.create_session("compression-parent", "telegram", session_key=route)
        db.end_session("compression-parent", "compression")
        db.create_session(
            "compression-child", "telegram", parent_session_id="compression-parent",
        )
        db.create_session("reset-parent", "telegram", session_key=route)
        db.end_session("reset-parent", "session_reset")
        db.create_session(
            "reset-child", "telegram", session_key=route, parent_session_id="reset-parent",
            model_config={"_reset_from": "reset-parent"},
        )

    monkeypatch.setattr(
        hermes_state, "SessionDB",
        lambda read_only=False: session_db_type(db_path=state_path, read_only=read_only),
    )
    ctx = SimpleNamespace(profile_name="iris", get_config=lambda key, default=None: default)
    receiver = ContinuationReceiver(ctx)
    base = {"source_session_key": route, "source_surface": "telegram"}
    assert receiver._route_target({**base, "source_session_id": "compression-parent"}) == "compression-child"
    assert receiver._route_target({**base, "source_session_id": "reset-parent"}) is None


@pytest.mark.asyncio
async def test_receiver_injects_complete_envelope_and_acknowledges_once(monkeypatch):
    class Client:
        def __init__(self):
            self.dispatched = []
            self.completed = []

        def pending_continuations(self, _limit, source_profile):
            assert source_profile == "iris"
            return {"items": [{
                "execution_id": "exec_1", "status": "QUEUED",
                "source_session_id": "session-1", "source_session_key": "route-1",
                "source_surface": "discord",
            }]}

        def claim_continuation(self, execution_id, consumer_id, lease_seconds):
            assert (execution_id, lease_seconds) == ("exec_1", 90)
            assert consumer_id.startswith("iris:")
            return {
                "lease_token": "lease-token-that-is-long-enough",
                "source_session_key": "route-1",
                "manifest": {
                    "decision_id": "dec_1", "submission_version": 1,
                    "responses": [{
                        "card_id": "card_1", "title": "Choose deployment",
                        "outcome": "recommended", "selected_option_id": "staged",
                        "selected_option_label": "Staged rollout",
                        "selected_option_details": "Canary first",
                        "selected_option_reason": "Safer",
                        "note": "Proceed",
                    }],
                },
            }

        def dispatch_continuation(self, execution_id, lease_token):
            self.dispatched.append((execution_id, lease_token))
            return {"status": "DISPATCHED"}

        def complete_continuation(self, execution_id, lease_token, result):
            self.completed.append((execution_id, lease_token, result))
            return {"status": "COMPLETED"}

    class Context:
        profile_name = "iris"

        def __init__(self):
            self.injected = []
            self.tasks = []

        def get_config(self, key, default=None):
            return default

        def inject_message(self, content, role="user", *, session_key=None):
            self.injected.append((content, session_key))
            return True

        def spawn_task(self, coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return task

    client = Client()
    ctx = Context()
    receiver = ContinuationReceiver(ctx)
    monkeypatch.setattr(receiver, "_client", lambda profile=None: client)
    monkeypatch.setattr(receiver, "_route_target", lambda _, profile=None: "session-1")
    monkeypatch.setattr(receiver, "ensure_started", lambda: None)

    assert await receiver.poll_once() is True
    prompt, route = ctx.injected[0]
    assert route == "route-1"
    assert "Decision-Inbox-Continuation: exec_1" in prompt
    assert "Selected option label: Staged rollout" in prompt
    assert "User note: Proceed" in prompt
    assert client.dispatched == [("exec_1", "lease-token-that-is-long-enough")]

    receiver.post_llm_call(
        session_id="session-1",
        user_message="Decision-Inbox-Continuation: exec_1\nAuthenticated Decision Inbox continuation",
        assistant_response="Deployment completed.",
    )
    await asyncio.gather(*ctx.tasks)
    assert len(client.completed) == 1
    assert client.completed[0][2]["assistant_response"] == "Deployment completed."


@pytest.mark.asyncio
async def test_receiver_keeps_profile_tokens_and_polls_separate_queues(monkeypatch):
    calls = []

    class Client:
        def __init__(self, profile):
            self.profile = profile

        def pending_continuations(self, limit, source_profile):
            calls.append((self.profile, limit, source_profile))
            return {"items": []}

    ctx = SimpleNamespace(
        profile_name="default",
        get_config=lambda key, default=None: default,
    )
    receiver = ContinuationReceiver(ctx)
    receiver.set_publish_token("default", "default-token")
    receiver.set_publish_token("iris", "iris-token")
    monkeypatch.setattr(receiver, "_client", lambda profile=None: Client(profile))

    assert await receiver.poll_once("default") is False
    assert await receiver.poll_once("iris") is False
    assert calls == [
        ("default", 10, "default"),
        ("iris", 10, "iris"),
    ]


def test_route_verification_rejects_cross_profile_item(monkeypatch):
    class SessionDB:
        def __init__(self, read_only=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_session(self, session_id):
            return {
                "id": session_id, "source": "telegram", "session_key": "route-1",
                "ended_at": None,
            }

        def get_compression_tip(self, session_id):
            return session_id

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=SessionDB))
    ctx = SimpleNamespace(
        profile_name="default",
        get_config=lambda key, default=None: default,
    )
    receiver = ContinuationReceiver(ctx)
    item = {
        "source_profile": "iris",
        "source_session_id": "session-1",
        "source_session_key": "route-1",
        "source_surface": "telegram",
    }
    assert receiver._route_target(item, "default") is None
    assert receiver._route_target(item, "iris") == "session-1"
