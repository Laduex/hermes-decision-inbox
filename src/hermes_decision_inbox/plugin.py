"""Hermes native-plugin registration entry point."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .cli import command, register_cli
from .schemas import READ_DECISIONS_SCHEMA, REQUEST_DECISION_SCHEMA, SESSION_DECISION_SCHEMA
from .tool import build_general_handler, build_handler, build_read_handler, check_available


_PROMPT = """## Weekly Memory Wiki Review Dashboard
Use `publish_weekly_wiki_review` only from the scheduled Yuna/default weekly Memory Wiki review.
Publish exactly one batch after the read-only review and Markdown fallback report are complete. The tool's top-level arguments are only `name` and `batch`; put card metadata and choices inside each batch item. Every card must contain an exact `wiki_patch_v1` operation and may include executable alternative wording.
After the tool returns `PUBLISHED`, return `[SILENT]`. The dashboard, not the cron session, owns review and application.
"""

_GENERAL_PROMPT = """## Session-bound Decision Inbox
Use `publish_decision` when the current Hermes task needs an explicit user choice before it can continue. Every card must include a concise summary describing what is being decided; it is shown as the short description on the inbox card. Publish only the decision data; the tool attaches the current profile, session, and task IDs. After it returns `PUBLISHED`, return `[SILENT]` and wait for the Decision Inbox to resume the exact session after the user applies the decision. Never use it from a delegated session; return the question to the parent agent instead. Archiving removes the request from the active Inbox without resuming the task.
"""

_READ_PROMPT = """## Read Decision Inbox
Use `read_decisions` when you need to check which decisions remain unresolved before publishing another decision or continuing related work. By default it reads only the current session and returns open decisions. Use profile scope only when you need the current profile's broader pending queue. This tool is read-only: never treat it as permission to apply, discard, archive, or publish a decision, and never expose another profile's queue.
"""


def register(ctx) -> None:
    """Register the tool, CLI, prompt policy, and explicit plugin skills."""
    ctx.register_tool(
        name="publish_weekly_wiki_review",
        toolset="decision_inbox",
        schema=REQUEST_DECISION_SCHEMA,
        handler=build_handler(ctx),
        check_fn=lambda: check_available(ctx),
        description="Publish the Sunday Memory Wiki review batch to Vaughn's private dashboard.",
        emoji="📬",
    )
    ctx.register_tool(
        name="publish_decision",
        toolset="decision_inbox",
        schema=SESSION_DECISION_SCHEMA,
        handler=build_general_handler(ctx),
        check_fn=lambda: check_available(ctx),
        description="Publish a session-bound decision for user review in the private dashboard.",
        emoji="📬",
    )
    ctx.register_tool(
        name="read_decisions",
        toolset="decision_inbox",
        schema=READ_DECISIONS_SCHEMA,
        handler=build_read_handler(ctx),
        check_fn=lambda: check_available(ctx),
        description="Read unresolved Decision Inbox entries for the current Hermes profile or session.",
        emoji="📬",
    )
    ctx.register_system_prompt_section(
        id="hermes-decision-inbox.routing",
        content=_PROMPT,
        position="after_memory",
        max_chars=1600,
    )
    ctx.register_system_prompt_section(
        id="hermes-decision-inbox.general-routing",
        content=_GENERAL_PROMPT,
        position="after_memory",
        max_chars=1600,
    )
    ctx.register_system_prompt_section(
        id="hermes-decision-inbox.read-routing",
        content=_READ_PROMPT,
        position="after_memory",
        max_chars=1600,
    )
    ctx.register_cli_command(
        name="decision-inbox",
        help="Weekly Wiki review dashboard diagnostics and test publishing",
        setup_fn=register_cli,
        handler_fn=command,
        description="Inspect or test the configured Decision Inbox service.",
    )
    skill_root = files("hermes_decision_inbox").joinpath("skills")
    if not skill_root.is_dir():
        skill_root = Path(__file__).resolve().parents[2] / "skills"
    for name in (
        "decision-inbox-routing",
        "decision-inbox-session-decisions",
        "decision-inbox-read-decisions",
        "decision-inbox-apply-resume-smoke",
        "memory-wiki-decision-applier",
    ):
        path = skill_root.joinpath(name, "SKILL.md")
        if path.is_file():
            ctx.register_skill(name=name, path=path, description=f"Decision Inbox: {name}.")
