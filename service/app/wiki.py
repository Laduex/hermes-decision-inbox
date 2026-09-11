"""Fail-closed implementation of the wiki_patch_v1 contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WikiApplyError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    }


def resolve_wiki_path(root: Path, target_path: str) -> Path:
    relative = Path(target_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise WikiApplyError(f"unsafe Wiki path: {target_path}")
    root = root.resolve()
    candidate = root.joinpath(relative)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WikiApplyError(f"symlink paths are not allowed: {target_path}")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise WikiApplyError(f"path escapes Wiki root: {target_path}") from exc
    return candidate


def _replace_section(text: str, anchor: str, replacement: str) -> str:
    lines = text.splitlines(keepends=True)
    index = next((i for i, line in enumerate(lines) if line.rstrip("\r\n") == anchor), None)
    if index is None:
        raise WikiApplyError(f"section anchor not found: {anchor}")
    heading = re.match(r"^(#{1,6})\s+", anchor)
    if not heading:
        raise WikiApplyError("section_anchor must be an exact Markdown heading")
    level = len(heading.group(1))
    end = len(lines)
    for i in range(index + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[i])
        if match and len(match.group(1)) <= level:
            end = i
            break
    body = replacement.rstrip() + "\n\n"
    return "".join(lines[:index]) + body + "".join(lines[end:])


def _render(current: str, operation: str, anchor: str | None, content: str) -> str:
    normalized = content.rstrip() + "\n"
    if operation == "create":
        return normalized
    if operation == "append":
        if anchor and anchor not in current:
            raise WikiApplyError(f"append anchor not found: {anchor}")
        return current.rstrip() + "\n\n" + normalized
    if operation == "replace_section":
        if not anchor:
            raise WikiApplyError("replace_section requires section_anchor")
        return _replace_section(current, anchor, normalized)
    raise WikiApplyError(f"unsupported operation: {operation}")


def _validate_markdown(root: Path, writes: dict[Path, str]) -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path, text in writes.items():
        if "\x00" in text:
            raise WikiApplyError(f"NUL byte in Markdown: {path.name}")
        for target in link_pattern.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean = target.split("#", 1)[0]
            linked = (path.parent / clean).resolve(strict=False)
            if linked not in writes and not linked.exists():
                raise WikiApplyError(f"broken internal link in {path.name}: {target}")


@dataclass
class WikiExecutor:
    wiki_root: Path
    backup_root: Path
    receipt_root: Path

    def apply_manifest(self, manifest_path: Path, expected_sha256: str,
                       approved_card_ids: list[str]) -> dict[str, Any]:
        manifest_path = Path(manifest_path)
        if sha256_file(manifest_path) != expected_sha256:
            raise WikiApplyError("manifest SHA-256 does not match")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "decision_manifest_v1" or manifest.get("status") != "SUBMITTED":
            raise WikiApplyError("manifest is not an immutable submitted decision manifest")
        approved = set(approved_card_ids)
        executable = []
        for response in manifest.get("responses", []):
            if response.get("card_id") not in approved:
                continue
            if response.get("outcome") not in {"recommended", "alternative"}:
                raise WikiApplyError("approved_card_ids includes a non-selected response")
            if response.get("execution_kind") != "wiki_patch_v1":
                raise WikiApplyError("manifest includes an unsupported execution kind")
            executable.append(response)
        if len(executable) != len(approved) or not executable:
            raise WikiApplyError("approved cards are missing from the manifest")

        self.wiki_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.receipt_root.mkdir(parents=True, exist_ok=True)
        targets: dict[str, Path] = {}
        writes: dict[Path, str] = {}
        originals: dict[Path, bytes | None] = {}
        before_tree = _tree_hashes(self.wiki_root)

        for response in executable:
            execution = response["execution"]
            path = resolve_wiki_path(self.wiki_root, execution["target_path"])
            if path.name == "decision-inbox-audit.md" and path.parent == self.wiki_root:
                raise WikiApplyError("decision-inbox-audit.md is reserved for verified apply receipts")
            targets[response["card_id"]] = path
            exists = path.exists()
            if exists and not path.is_file():
                raise WikiApplyError(f"target is not a regular file: {execution['target_path']}")
            if execution["operation"] == "create" and exists:
                raise WikiApplyError(f"create target already exists: {execution['target_path']}")
            if execution["operation"] != "create" and not exists:
                raise WikiApplyError(f"target does not exist: {execution['target_path']}")
            current_bytes = path.read_bytes() if exists else None
            originals[path] = current_bytes
            actual_hash = hashlib.sha256(current_bytes or b"").hexdigest()
            expected = execution.get("base_sha256")
            if expected and actual_hash != expected:
                raise WikiApplyError(f"base SHA-256 changed: {execution['target_path']}")
            current = (current_bytes or b"").decode("utf-8")
            writes[path] = _render(current, execution["operation"], execution.get("section_anchor"),
                                   execution["proposed_content"])

        audit_path = resolve_wiki_path(self.wiki_root, "decision-inbox-audit.md")
        audit_bytes = audit_path.read_bytes() if audit_path.exists() else None
        originals[audit_path] = audit_bytes
        audit_entry = (
            f"- {datetime.now(timezone.utc).isoformat(timespec='seconds')} — applied manifest "
            f"`{expected_sha256}`; cards: {', '.join(sorted(approved))}\n"
        )
        audit_text = (audit_bytes or b"# Decision Inbox Apply Log\n\n").decode("utf-8")
        writes[audit_path] = audit_text.rstrip() + "\n" + audit_entry
        _validate_markdown(self.wiki_root, writes)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.backup_root / f"wiki-{stamp}-{expected_sha256[:12]}.tar.gz"
        with tarfile.open(backup, "w:gz") as archive:
            for path, content in originals.items():
                if content is not None:
                    archive.add(path, arcname=str(path.relative_to(self.wiki_root)), recursive=False)
        os.chmod(backup, 0o600)

        try:
            for path, content in writes.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
            after_tree = _tree_hashes(self.wiki_root)
            expected_changes = {str(path.relative_to(self.wiki_root)) for path in writes}
            changed = {key for key in before_tree.keys() | after_tree.keys() if before_tree.get(key) != after_tree.get(key)}
            if changed - expected_changes:
                raise WikiApplyError(f"unapproved Wiki files changed: {sorted(changed - expected_changes)}")
        except Exception:
            for path, content in originals.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    os.chmod(path, 0o600)
            raise

        receipt = {
            "status": "success", "manifest_sha256": expected_sha256,
            "backup_path": str(backup), "audit_log": str(audit_path), "applied": [
                {"card_id": card_id, "target_path": str(path.relative_to(self.wiki_root)),
                 "sha256": sha256_file(path)} for card_id, path in targets.items()
            ], "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        receipt_path = self.receipt_root / f"{expected_sha256}.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2), encoding="utf-8")
        os.chmod(receipt_path, 0o600)
        receipt["receipt_path"] = str(receipt_path)
        return receipt
