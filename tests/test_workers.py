from __future__ import annotations

import json

import pytest

from service.app.integrations import HermesClient, decision_resume_prompt
from service.app.workers import Workers


class FakeHermes:
    def __init__(self, *, recoverable: bool = True):
        self.recoverable = recoverable
        self.calls: list[tuple] = []

    async def require_session(self, profile: str, session_id: str) -> None:
        self.calls.append(("require", profile, session_id))
        if not self.recoverable:
            raise RuntimeError("The original Hermes session cannot be recovered")

    async def start_run(self, profile: str, prompt: str, key: str, session_id: str | None = None) -> str:
        self.calls.append(("start", profile, session_id, key, prompt))
        return "run_test"

    async def wait_run(self, profile: str, run_id: str) -> dict:
        self.calls.append(("wait", profile, run_id))
        return {"status": "completed", "id": run_id}


def submitted_ordinary(db, single_request, *, outcome: str = "recommended") -> str:
    decision_id = db.publish(single_request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, outcome, None, "user note", 424242)
    db.submit(decision_id, decision["version"], 424242)
    return decision_id


@pytest.mark.asyncio
async def test_worker_resumes_exact_profile_and_session_once(db, settings, single_request):
    decision_id = submitted_ordinary(db, single_request)
    workers = Workers(db, settings)
    fake = FakeHermes()
    workers.hermes = fake
    assert await workers._execution_once() is True
    assert fake.calls[0] == ("require", "iris", "session-1")
    start = fake.calls[1]
    assert start[:4] == ("start", "iris", "session-1", f"decision-resume:{decision_id}:1")
    assert "Authenticated Decision Inbox response" in start[4]
    assert db.get_decision(decision_id)["status"] == "ARCHIVED"
    assert await workers._execution_once() is False


@pytest.mark.asyncio
async def test_missing_original_session_blocks_without_starting_new_one(db, settings, single_request):
    decision_id = submitted_ordinary(db, single_request)
    workers = Workers(db, settings)
    fake = FakeHermes(recoverable=False)
    workers.hermes = fake
    assert await workers._execution_once() is True
    assert [call[0] for call in fake.calls] == ["require"]
    assert db.get_decision(decision_id)["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_worker_applies_selected_wiki_patch_without_hermes_run(db, settings, single_request):
    request = dict(single_request)
    request["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "weekly-canary.md", "operation": "create",
        "base_sha256": None, "section_anchor": None, "proposed_content": "# Weekly Canary",
    }
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    submitted = db.submit(decision_id, decision["version"], 424242)
    db.queue_apply(submitted["manifest_id"], submitted["decision_version"])
    workers = Workers(db, settings)
    fake = FakeHermes()
    workers.hermes = fake
    assert await workers._execution_once() is True
    assert fake.calls == []
    assert (settings.wiki_root / "weekly-canary.md").read_text() == "# Weekly Canary\n"
    assert db.get_decision(decision_id)["status"] == "ARCHIVED"


def test_rejected_prompt_grants_no_alternative(single_request):
    manifest = {
        "decision_id": "dec_test", "submission_version": 1,
        "responses": [{
            "card_id": "card_test", "title": single_request["title"], "outcome": "rejected",
            "selected_option_id": None, "note": "No deployment yet",
        }],
    }
    prompt = decision_resume_prompt(manifest)
    assert "Selected outcome: rejected" in prompt
    assert "Selected option: none" in prompt


def test_applied_smoke_decision_resumes_with_received_choice(db, settings):
    request = {
        "name": "decision-inbox-apply-resume-smoke",
        "title": "Did you receive this Decision Inbox smoke test?",
        "summary": "Confirm that applying this card resumes the originating session.",
        "details": "This is a harmless end-to-end receipt test; it does not change Wiki or external data.",
        "recommendation": {
            "option_id": "received",
            "label": "Yes, I received it",
            "reason": "Confirms that Apply returns the user's choice to the originating session.",
        },
        "alternatives": [],
        "evidence": [],
        "priority": "normal",
        "source_profile": "iris",
        "source_session_id": "session-smoke",
        "source_task_id": "task-smoke",
        "plugin_version": "1.5.0",
    }
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    submitted = db.submit(decision_id, decision["version"], 424242)
    manifest_path = settings.manifest_root / decision_id / f"{submitted['sha256']}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    prompt = decision_resume_prompt(manifest)
    assert "Did you receive this Decision Inbox smoke test?" in prompt
    assert "Selected outcome: recommended" in prompt
    assert "Selected option: received" in prompt


def test_hermes_client_uses_profile_scoped_multiplex_routes():
    client = HermesClient("http://hermes:8642", {"default": "default-key", "iris": "iris-key"})
    assert client._url("default", "/v1/runs") == "http://hermes:8642/v1/runs"
    assert client._url("iris", "/v1/runs") == "http://hermes:8642/p/iris/v1/runs"
    assert client._url("profile with spaces", "/v1/runs") == (
        "http://hermes:8642/p/profile%20with%20spaces/v1/runs"
    )
