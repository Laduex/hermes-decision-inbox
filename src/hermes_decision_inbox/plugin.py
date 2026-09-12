"""Hermes native-plugin registration entry point."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .cli import command, register_cli
from .schemas import REQUEST_DECISION_SCHEMA
from .tool import build_handler, check_available


_PROMPT = """## Weekly Memory Wiki Review Dashboard
Use `publish_weekly_wiki_review` only from the scheduled Yuna/default weekly Memory Wiki review.
Publish exactly one batch after the read-only review and Markdown fallback report are complete. The tool's top-level arguments are only `name` and `batch`; put card metadata and choices inside each batch item. Every card must contain an exact `wiki_patch_v1` operation and may include executable alternative wording.
After the tool returns `PUBLISHED`, return `[SILENT]`. The dashboard, not the cron session, owns review and application.
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
    ctx.register_system_prompt_section(
        id="hermes-decision-inbox.routing",
        content=_PROMPT,
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
    for name in ("decision-inbox-routing", "memory-wiki-decision-applier"):
        path = skill_root.joinpath(name, "SKILL.md")
        if path.is_file():
            ctx.register_skill(name=name, path=path, description=f"Decision Inbox: {name}.")
