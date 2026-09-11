---
name: memory-wiki-decision-applier
description: Apply selected weekly Wiki cards from an immutable manifest.
version: 0.2.0
author: Vaughn Dazo, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [wiki, decisions, apply, backup, receipt]
    related_skills: [decision-inbox-routing]
    requires_toolsets: [terminal]
---

# Memory Wiki Decision Applier Skill

Apply only explicitly selected `wiki_patch_v1` cards from an immutable Decision Inbox manifest. A selected card may use the recommendation or an exact executable alternative. Rejected and abstained cards never change the Wiki.

## When to Use

- Use only after the user presses Apply on the private weekly dashboard.
- Use only for a manifest path, SHA-256, approved card IDs, backup path, and receipt destination supplied by the companion service.
- Do not use while generating a weekly review or for non-Wiki actions.

## Prerequisites

- `hermes-decision-inbox` is installed and enabled.
- The manifest and Wiki are mounted read/write in the deterministic companion-service worker.
- The approved Wiki root resolves beneath `/opt/data/home/wiki` in production.

## How to Run

Use `terminal` to invoke the packaged validator and applier:

```text
decision-inbox apply-manifest --manifest <path> --sha256 <sha256> --approved-card <id> --wiki-root /opt/data/home/wiki --backup-root <backup-path> --receipt-root <receipt-path>
```

Repeat `--approved-card` for every approved card ID. Never construct or edit the manifest inside the worker.

## Quick Reference

| Result | Meaning |
| --- | --- |
| `success` | Every approved card applied and has a receipt |
| Preflight error | No Wiki files changed |
| Apply error | Original files were restored from the complete backup |

## Procedure

1. Read the supplied parameters without changing them. Stop if any required value is absent.
2. Run the packaged `apply-manifest` command through `terminal` with the exact parameters.
3. The command verifies the manifest hash, approved outcomes, path containment, symlinks, operations, and base hashes before writing.
4. The command creates a mode-600 backup, applies atomically, checks internal links, and rejects unapproved file changes.
5. Read the returned receipt. Report the exact applied card IDs, backup path, receipt path, and any blocker.
6. Never retry with a changed hash or reduced validation. A conflict requires a new review batch.

## Pitfalls

- A target changed after review blocks the entire batch.
- Absolute paths, traversal, and symlinks fail closed.
- Rejected and abstained cards cannot appear in `--approved-card`. A selected alternative must carry its own exact Wiki patch.
- Do not repair Markdown manually after a failed apply; preserve the rollback evidence.

## Verification

- The manifest SHA-256 matches the immutable submission.
- Every changed file belongs to an approved card.
- The backup and per-card receipt exist with mode 600.
- On failure, hashes match the pre-apply Wiki state.
