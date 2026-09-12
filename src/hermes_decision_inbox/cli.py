"""CLI diagnostics for the Hermes plugin and companion service."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

from . import __version__


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="decision_inbox_command")
    health = subs.add_parser("health", help="Check companion-service health")
    health.add_argument("--url", default=os.environ.get("DECISION_INBOX_SERVICE_URL", "http://127.0.0.1:8080"))
    publish = subs.add_parser("publish-test", help="Publish a harmless synthetic weekly Wiki card")
    publish.add_argument("--url", default=os.environ.get("DECISION_INBOX_SERVICE_URL", "http://127.0.0.1:8080"))
    publish.add_argument("--profile", default="default")
    publish.add_argument("--session", default="local-test-session")
    apply_parser = subs.add_parser("apply-manifest", help="Apply an immutable wiki_patch_v1 manifest")
    apply_parser.add_argument("--manifest", required=True)
    apply_parser.add_argument("--sha256", required=True)
    apply_parser.add_argument("--approved-card", action="append", required=True)
    apply_parser.add_argument("--wiki-root", required=True)
    apply_parser.add_argument("--backup-root", required=True)
    apply_parser.add_argument("--receipt-root", required=True)
    parser.set_defaults(func=command)


def _request(url: str, *, body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("DECISION_INBOX_PUBLISH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode())


def command(args: argparse.Namespace) -> int:
    sub = getattr(args, "decision_inbox_command", None)
    try:
        if sub == "health":
            result = _request(f"{args.url.rstrip('/')}/healthz")
        elif sub == "publish-test":
            result = _request(f"{args.url.rstrip('/')}/internal/v1/decisions", body={
                "name": "local-weekly-wiki-canary",
                "batch": [{
                    "title": "Create the disposable canary note?",
                    "summary": "Tests weekly Wiki card publication against a disposable fixture.",
                    "details": "Nothing is written until this card is accepted and the batch is explicitly applied.",
                    "recommendation": {
                        "option_id": "create_note",
                        "label": "Create the canary note",
                        "reason": "Validates the guarded Wiki apply path.",
                    },
                    "alternatives": [],
                    "evidence": [{"label": "Scope", "value": "Disposable local Wiki only"}],
                    "priority": "low",
                    "execution": {
                        "execution_kind": "wiki_patch_v1",
                        "target_path": "Decision Inbox Canary.md",
                        "operation": "create",
                        "base_sha256": None,
                        "section_anchor": None,
                        "proposed_content": "# Decision Inbox Canary\n\nCreated by the private weekly dashboard canary.\n",
                    },
                }],
                "source_profile": args.profile,
                "source_session_id": args.session,
                "source_task_id": args.session,
                "plugin_version": __version__,
            })
        elif sub == "apply-manifest":
            from pathlib import Path
            from service.app.wiki import WikiExecutor
            result = WikiExecutor(
                Path(args.wiki_root), Path(args.backup_root), Path(args.receipt_root)
            ).apply_manifest(Path(args.manifest), args.sha256, args.approved_card)
        else:
            print("usage: hermes decision-inbox {health,publish-test,apply-manifest}")
            return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="decision-inbox")
    register_cli(parser)
    raise SystemExit(command(parser.parse_args()))
