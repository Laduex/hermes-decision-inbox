"""Versioned, authenticated continuation envelopes shared by both delivery paths."""

from __future__ import annotations

from typing import Any


CONTINUATION_SCHEMA = "decision_continuation_v1"
CONTINUATION_MARKER = "Decision-Inbox-Continuation"


def decision_continuation_prompt(
    manifest: dict[str, Any], execution_id: str | None = None,
) -> str:
    """Build a complete continuation without repeating the original task prompt."""
    marker = execution_id or "runs-api-fallback"
    lines = [
        f"{CONTINUATION_MARKER}: {marker}",
        "Authenticated Decision Inbox response",
        "Authenticated Decision Inbox continuation",
        f"Continuation schema: {CONTINUATION_SCHEMA}",
        f"Decision ID: {manifest['decision_id']}",
        f"Manifest version: {manifest['submission_version']}",
    ]
    for response in manifest["responses"]:
        if response["outcome"] == "deferred":
            continue
        lines.extend([
            "",
            f"Card: {response['title']} ({response['card_id']})",
            f"Selected outcome: {response['outcome']}",
            f"Selected option: {response.get('selected_option_id') or 'none'}",
            f"Selected option ID: {response.get('selected_option_id') or 'none'}",
            f"Selected option label: {response.get('selected_option_label') or 'none'}",
            f"Selected option details: {response.get('selected_option_details') or 'none'}",
            f"Selected option reason: {response.get('selected_option_reason') or 'none'}",
            f"User note: {response.get('note') or 'none'}",
        ])
    lines.extend([
        "",
        "Continue the original task from the existing session history using every response above.",
        "Do not repeat the original prompt, ask the same decision again, or broaden the task's scope.",
        "Return the full continuation result through this conversation's normal delivery path.",
    ])
    return "\n".join(lines)


__all__ = ["CONTINUATION_MARKER", "CONTINUATION_SCHEMA", "decision_continuation_prompt"]
