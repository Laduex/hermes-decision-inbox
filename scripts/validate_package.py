#!/usr/bin/env python3
"""Validate the standalone plugin manifest and bundled skill metadata."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MANIFEST = {"manifest_version", "api_version", "name", "version", "description", "author"}


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter")
    return yaml.safe_load(match.group(1)) or {}


def main() -> int:
    errors: list[str] = []
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8")) or {}
    missing = REQUIRED_MANIFEST - manifest.keys()
    if missing:
        errors.append(f"plugin.yaml missing fields: {sorted(missing)}")
    if manifest.get("name") != "hermes-decision-inbox":
        errors.append("plugin name must be hermes-decision-inbox")
    if manifest.get("author") != "Vaughn Dazo, Hermes Agent":
        errors.append("plugin author credit is incorrect")

    skill_names: set[str] = set()
    skill_metadata: list[tuple[Path, dict]] = []
    for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
        try:
            metadata = frontmatter(path)
        except Exception as exc:
            errors.append(str(exc))
            continue
        skill_metadata.append((path, metadata))
        name = str(metadata.get("name") or "")
        if not name or name in skill_names:
            errors.append(f"{path}: missing or duplicate skill name")
        skill_names.add(name)
        if metadata.get("author") != "Vaughn Dazo, Hermes Agent":
            errors.append(f"{path}: author credit is incorrect")
        description = str(metadata.get("description") or "")
        if not description or len(description) > 120:
            errors.append(f"{path}: description must be 1-120 characters")

    for path, metadata in skill_metadata:
        related = (((metadata.get("metadata") or {}).get("hermes") or {}).get("related_skills") or [])
        missing_related = sorted(set(map(str, related)) - skill_names)
        if missing_related:
            errors.append(f"{path}: unknown related_skills {missing_related}")

    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Validated plugin manifest and {len(skill_names)} skills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
