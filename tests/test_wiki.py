from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from service.app.wiki import WikiApplyError, WikiExecutor, sha256_file


def manifest(tmp_path: Path, response: dict) -> tuple[Path, str]:
    payload = {
        "schema": "decision_manifest_v1", "status": "SUBMITTED", "decision_id": "dec_test", "submission_version": 1,
        "source_profile": "default", "source_session_id": "session", "telegram_user_id": 424242,
        "created_at": "2026-09-11T00:00:00+00:00", "responses": [response],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "manifest.json"
    path.write_bytes(encoded)
    return path, hashlib.sha256(encoded).hexdigest()


def executor(tmp_path: Path) -> WikiExecutor:
    return WikiExecutor(tmp_path / "wiki", tmp_path / "backups", tmp_path / "receipts")


def test_applies_only_approved_card_and_writes_receipt(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = wiki / "memory.md"
    target.write_text("# Memory\n\nOld\n")
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Update memory", "outcome": "recommended",
        "selected_option_id": "approve", "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "replace_section",
                      "base_sha256": sha256_file(target), "section_anchor": "# Memory",
                      "proposed_content": "# Memory\n\nNew"},
    }
    path, digest = manifest(tmp_path, response)
    receipt = executor(tmp_path).apply_manifest(path, digest, ["card-1"])
    assert target.read_text() == "# Memory\n\nNew\n\n"
    assert Path(receipt["backup_path"]).is_file()
    assert oct(Path(receipt["backup_path"]).stat().st_mode & 0o777) == "0o600"
    assert Path(receipt["receipt_path"]).is_file()
    assert oct(Path(receipt["receipt_path"]).stat().st_mode & 0o777) == "0o600"


def test_hash_conflict_blocks_before_mutation(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = wiki / "memory.md"
    target.write_text("current")
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Append", "outcome": "recommended",
        "selected_option_id": "approve", "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
                      "base_sha256": "0" * 64, "section_anchor": None, "proposed_content": "next"},
    }
    path, digest = manifest(tmp_path, response)
    with pytest.raises(WikiApplyError, match="base SHA-256 changed"):
        executor(tmp_path).apply_manifest(path, digest, ["card-1"])
    assert target.read_text() == "current"


@pytest.mark.parametrize("target", ["../outside.md", "/tmp/outside.md"])
def test_path_escape_fails_closed(tmp_path, target):
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Escape", "outcome": "recommended",
        "selected_option_id": "approve", "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": target, "operation": "create",
                      "base_sha256": None, "section_anchor": None, "proposed_content": "# Nope"},
    }
    path, digest = manifest(tmp_path, response)
    with pytest.raises(WikiApplyError, match="unsafe Wiki path"):
        executor(tmp_path).apply_manifest(path, digest, ["card-1"])


def test_symlink_target_fails_closed(tmp_path):
    wiki = tmp_path / "wiki"
    outside = tmp_path / "outside"
    wiki.mkdir(); outside.mkdir()
    os.symlink(outside, wiki / "linked")
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Symlink", "outcome": "recommended",
        "selected_option_id": "approve", "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": "linked/nope.md", "operation": "create",
                      "base_sha256": None, "section_anchor": None, "proposed_content": "# Nope"},
    }
    path, digest = manifest(tmp_path, response)
    with pytest.raises(WikiApplyError, match="symlink"):
        executor(tmp_path).apply_manifest(path, digest, ["card-1"])
    assert not (outside / "nope.md").exists()


def test_declined_card_cannot_be_approved(tmp_path):
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Declined", "outcome": "rejected",
        "selected_option_id": None, "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": "nope.md", "operation": "create",
                      "base_sha256": None, "section_anchor": None, "proposed_content": "# Nope"},
    }
    path, digest = manifest(tmp_path, response)
    with pytest.raises(WikiApplyError, match="non-selected"):
        executor(tmp_path).apply_manifest(path, digest, ["card-1"])


def test_selected_alternative_patch_can_be_applied(tmp_path):
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Alternative", "outcome": "alternative",
        "selected_option_id": "short", "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": "alternative.md", "operation": "create",
                      "base_sha256": None, "section_anchor": None, "proposed_content": "# Short"},
    }
    path, digest = manifest(tmp_path, response)
    executor(tmp_path).apply_manifest(path, digest, ["card-1"])
    assert (tmp_path / "wiki" / "alternative.md").read_text() == "# Short\n"


def test_write_failure_restores_every_touched_file(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = wiki / "memory.md"
    target.write_text("# Memory\n\nOriginal\n")
    original = target.read_bytes()
    response = {
        "card_id": "card-1", "card_version": 1, "title": "Append", "outcome": "recommended",
        "selected_option_id": "approve", "note": "", "execution_kind": "wiki_patch_v1",
        "execution": {"execution_kind": "wiki_patch_v1", "target_path": "memory.md", "operation": "append",
                      "base_sha256": sha256_file(target), "section_anchor": None,
                      "proposed_content": "New memory"},
    }
    path, digest = manifest(tmp_path, response)
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic write failure")
        real_replace(source, destination)

    monkeypatch.setattr("service.app.wiki.os.replace", fail_second_replace)
    with pytest.raises(OSError, match="synthetic write failure"):
        executor(tmp_path).apply_manifest(path, digest, ["card-1"])
    assert target.read_bytes() == original
    assert not (wiki / "decision-inbox-audit.md").exists()
