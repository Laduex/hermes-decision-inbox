import json
from pathlib import Path

import pytest

from service.app.database import ConflictError, Database, StateError


def test_publish_deduplicates_within_window(db, single_request):
    first = db.publish(single_request, "iris")
    second = db.publish(single_request, "iris")
    assert second == {"decision_id": first["decision_id"], "deduplicated": True, "merged": False}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


def test_profile_credential_cannot_impersonate(db, single_request):
    with pytest.raises(PermissionError):
        db.publish(single_request, "default")


def test_deferred_only_submission_does_not_queue_resume(db, single_request):
    published = db.publish(single_request, "iris")
    decision = db.get_decision(published["decision_id"])
    card = decision["cards"][0]
    decision = db.respond(card["card_id"], card["version"], "deferred", None, "later", 424242)
    result = db.submit(decision["decision_id"], decision["version"], 424242)
    assert result["status"] == "DEFERRED"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0


def test_ordinary_submission_is_immutable_and_idempotently_queued(db, single_request):
    published = db.publish(single_request, "iris")
    decision = db.get_decision(published["decision_id"])
    card = decision["cards"][0]
    decision = db.respond(card["card_id"], card["version"], "recommended", None, "", 424242)
    result = db.submit(decision["decision_id"], decision["version"], 424242)
    assert result["status"] == "QUEUED_FOR_RESUME"
    manifest_path = Path(db.manifest_root) / decision["decision_id"] / f"{result['sha256']}.json"
    assert manifest_path.is_file()
    assert oct(manifest_path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises((ConflictError, StateError)):
        db.submit(decision["decision_id"], decision["version"], 424242)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 1


def test_auto_resume_can_be_disabled_for_interface_canary(settings, single_request):
    db = Database(settings.database_path, settings.manifest_root, auto_resume=False)
    db.initialize()
    decision_id = db.publish(single_request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    result = db.submit(decision_id, decision["version"], 424242)
    assert result["status"] == "COMPLETED"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0


def test_discard_closes_open_decision_without_queueing_resume(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    discarded = db.discard(decision_id, decision["version"], 424242)
    assert discarded["status"] == "CANCELLED"
    assert all(card["status"] == "CANCELLED" for card in discarded["cards"])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT event_type FROM audit_events WHERE decision_id=? ORDER BY event_id DESC LIMIT 1",
            (decision_id,),
        ).fetchone()[0] == "decision_discarded"


def test_archive_moves_open_decision_to_archive_without_cancelling_cards(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    archived = db.archive(decision_id, decision["version"], 424242)
    assert archived["status"] == "ARCHIVED"
    assert archived["cards"][0]["status"] == "READY"
    assert [item["decision_id"] for item in db.inbox("all")] == []
    assert [item["decision_id"] for item in db.inbox("archive")] == [decision_id]


def test_published_profile_policy_overrides_service_auto_resume(db, single_request):
    request = dict(single_request)
    request["auto_resume"] = False
    request["default_expiry_days"] = 3
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    assert decision["auto_resume"] == 0
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    result = db.submit(decision_id, decision["version"], 424242)
    assert result["status"] == "COMPLETED"


def test_claimed_resume_moves_decision_to_resuming(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    db.submit(decision_id, decision["version"], 424242)
    claimed = db.claim_execution()
    assert claimed is not None
    assert db.get_decision(decision_id)["status"] == "RESUMING"


def test_stale_card_version_is_rejected(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    card = db.get_decision(decision_id)["cards"][0]
    with pytest.raises(ConflictError):
        db.respond(card["card_id"], card["version"] + 1, "rejected", None, "", 424242)


def test_additional_session_request_merges_into_open_batch(db, single_request):
    first = db.publish(single_request, "iris")
    changed = dict(single_request)
    changed["title"] = "Choose a release window"
    changed["summary"] = "A release window is required."
    changed["details"] = "The current session is already waiting on a related deployment choice."
    second = db.publish(changed, "iris")
    assert second["decision_id"] == first["decision_id"]
    assert second["merged"] is True
    assert len(db.get_decision(first["decision_id"])["cards"]) == 2
    repeated = db.publish(changed, "iris")
    assert repeated["deduplicated"] is True
    assert len(db.get_decision(first["decision_id"])["cards"]) == 2


def test_inflight_session_request_is_rejected_with_existing_id(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    db.submit(decision_id, decision["version"], 424242)
    changed = dict(single_request)
    changed["title"] = "Choose another deployment detail"
    with pytest.raises(ConflictError, match=decision_id):
        db.publish(changed, "iris")


def test_session_cannot_mix_wiki_and_ordinary_cards(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    wiki = dict(single_request)
    wiki["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
        "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Proposed memory",
    }
    with pytest.raises(ConflictError, match=decision_id):
        db.publish(wiki, "iris")


def test_deferred_card_can_be_submitted_later_without_replaying_prior_card(db, single_request):
    request = dict(single_request)
    second_card = {
        "title": "Choose the release time", "summary": "Select a safe release time.",
        "details": "This can be deferred independently.",
        "recommendation": {"option_id": "morning", "label": "Morning", "reason": "Staffed window"},
        "alternatives": [{"option_id": "night", "label": "Night", "details": "Lower traffic"}],
        "priority": "normal", "evidence": [],
    }
    request.pop("title"); request.pop("summary"); request.pop("details")
    request.pop("recommendation"); request.pop("alternatives"); request.pop("evidence")
    request["batch"] = [
        {k: single_request[k] for k in ("title", "summary", "details", "recommendation", "alternatives", "evidence", "priority")},
        second_card,
    ]
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    decision = db.respond(decision["cards"][1]["card_id"], 1, "deferred", None, "", 424242)
    first = db.submit(decision_id, decision["version"], 424242)
    assert first["status"] == "QUEUED_FOR_RESUME"
    with db.connect() as conn:
        execution = conn.execute("SELECT execution_id FROM execution_attempts").fetchone()[0]
    db.finish_execution(execution, True, {"status": "completed"})
    deferred = db.inbox("deferred")
    assert [item["decision_id"] for item in deferred] == [decision_id]
    decision = db.get_decision(decision_id)
    deferred_card = next(card for card in decision["cards"] if card["response"]["outcome"] == "deferred")
    decision = db.respond(deferred_card["card_id"], 1, "recommended", None, "now", 424242)
    second = db.submit(decision_id, decision["version"], 424242)
    assert second["status"] == "QUEUED_FOR_RESUME"
    assert second["sha256"] != first["sha256"]
    with db.connect() as conn:
        manifests = [json.loads(row[0]) for row in conn.execute(
            "SELECT manifest_json FROM submission_manifests WHERE decision_id=? ORDER BY submission_version", (decision_id,))]
    assert len(manifests[0]["responses"]) == 2
    assert [row["card_id"] for row in manifests[1]["responses"]] == [deferred_card["card_id"]]


def test_wiki_alternative_without_exact_patch_is_recorded_but_not_executed(db, single_request):
    request = dict(single_request)
    request["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
        "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Proposed memory",
    }
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    card = decision["cards"][0]
    decision = db.respond(card["card_id"], 1, "alternative", "direct", "Do this manually", 424242)
    result = db.submit(decision_id, decision["version"], 424242)
    assert result["status"] == "COMPLETED"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0


def test_wiki_alternative_with_exact_patch_is_ready_to_apply(db, single_request):
    request = dict(single_request)
    request["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
        "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Recommended memory",
    }
    request["alternatives"] = [{
        "option_id": "direct", "label": "Use shorter wording", "details": "Apply concise wording.",
        "execution": {
            "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
            "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Short memory",
        },
    }]
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    assert decision["cards"][0]["options"][1]["execution"]["proposed_content"] == "Short memory"
    decision = db.respond(decision["cards"][0]["card_id"], 1, "alternative", "direct", "", 424242)
    result = db.submit(decision_id, decision["version"], 424242)
    assert result["status"] == "READY_TO_APPLY"
    manifest = json.loads((Path(db.manifest_root) / decision_id / f"{result['sha256']}.json").read_text())
    assert manifest["responses"][0]["execution"]["proposed_content"] == "Short memory"


def test_abstained_wiki_card_is_archived_without_execution(db, single_request):
    request = dict(single_request)
    request["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
        "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Proposed memory",
    }
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "abstained", None, "", 424242)
    result = db.submit(decision_id, decision["version"], 424242)
    assert result["status"] == "COMPLETED"
    assert result["abstained"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0] == 0


def test_weekly_only_database_rejects_non_batch_requests(settings, single_request):
    restricted = Database(settings.database_path, settings.manifest_root, weekly_only=True)
    restricted.initialize()
    with pytest.raises(ValueError, match="only batched Weekly Memory Wiki reviews"):
        restricted.publish(single_request, "iris")


def test_wiki_recommendation_requires_versioned_apply_confirmation(db, single_request):
    request = dict(single_request)
    request["execution"] = {
        "execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
        "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "Proposed memory",
    }
    decision_id = db.publish(request, "iris")["decision_id"]
    decision = db.get_decision(decision_id)
    card = decision["cards"][0]
    decision = db.respond(card["card_id"], 1, "recommended", None, "", 424242)
    result = db.submit(decision_id, decision["version"], 424242)
    assert result["status"] == "READY_TO_APPLY"
    with pytest.raises(ConflictError):
        db.queue_apply(result["manifest_id"], result["decision_version"] + 1)
    execution = db.queue_apply(result["manifest_id"], result["decision_version"])
    assert execution["kind"] == "wiki_apply"
    repeated = db.queue_apply(result["manifest_id"], result["decision_version"] + 1)
    assert repeated["execution_id"] == execution["execution_id"]


def test_restart_recovers_claimed_outbox_and_execution(db, single_request):
    decision_id = db.publish(single_request, "iris")["decision_id"]
    claimed_notification = db.claim_notification()
    assert claimed_notification is not None
    decision = db.get_decision(decision_id)
    decision = db.respond(decision["cards"][0]["card_id"], 1, "recommended", None, "", 424242)
    db.submit(decision_id, decision["version"], 424242)
    execution = db.claim_execution()
    assert execution is not None
    db.initialize()
    with db.connect() as conn:
        assert conn.execute(
            "SELECT status FROM notification_outbox WHERE notification_id=?",
            (claimed_notification["notification_id"],),
        ).fetchone()[0] == "RETRY"
        assert conn.execute(
            "SELECT status FROM execution_attempts WHERE execution_id=?", (execution["execution_id"],),
        ).fetchone()[0] == "QUEUED"
