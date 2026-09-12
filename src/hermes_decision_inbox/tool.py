"""Hermes tool handler for durable decision publication."""

from __future__ import annotations

import json
import os
from typing import Any

from . import __version__
from .client import DecisionInboxClient, DecisionInboxError
from .schemas import (
    READ_DECISIONS_SCHEMA,
    REQUEST_DECISION_SCHEMA,
    ReadDecisionsRequest,
    SESSION_DECISION_SCHEMA,
    SessionDecisionRequest,
    WeeklyWikiReviewRequest,
)


def _tool_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name, "").strip()
    return str(get_secret(name, "") or "").strip()


def _session_is_delegated(session_id: str) -> bool:
    """Use Hermes's durable session marker; unknown sessions are handled by continuation preflight."""
    try:
        from hermes_state import SessionDB
    except ImportError:
        return False
    try:
        with SessionDB(read_only=True) as db:
            row = db.get_session(session_id)
        if not row:
            return False
        raw_config = row.get("model_config") or "{}"
        config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        return str(row.get("source") or "").lower() == "subagent" or bool(
            isinstance(config, dict) and config.get("_delegate_from")
        )
    except Exception:
        return False


def _config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean setting must be true or false")


def _build_publisher(ctx, request_type, *, weekly: bool):
    """Bind profile-aware configuration without storing cross-profile state."""
    def handle(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            request = request_type.model_validate(args)
        except Exception as exc:
            return _tool_result({"status": "ERROR", "error": f"Invalid decision request: {exc}"})

        service_url = str(ctx.get_config("service_url", default="http://decision-inbox:8080") or "").strip()
        token = _secret("DECISION_INBOX_PUBLISH_TOKEN")
        if not service_url or not token:
            return _tool_result({
                "status": "ERROR",
                "error": "Decision Inbox is not configured; ask in ordinary chat instead.",
            })
        profile = ctx.profile_name or "default"
        if weekly and profile != "default":
            return _tool_result({
                "status": "ERROR",
                "error": "Only Yuna/default may publish the weekly Memory Wiki review.",
            })
        if not weekly:
            try:
                enabled = _config_bool(ctx.get_config("general_decisions_enabled", default=True), True)
            except ValueError as exc:
                return _tool_result({"status": "ERROR", "error": f"Decision Inbox configuration error: {exc}"})
            if not enabled:
                return _tool_result({
                    "status": "ERROR",
                    "error": "General Decision Inbox publishing is disabled for this profile.",
                })
        else:
            try:
                wiki_enabled = _config_bool(ctx.get_config("wiki_executor_enabled", default=True), True)
            except ValueError as exc:
                return _tool_result({"status": "ERROR", "error": f"Decision Inbox configuration error: {exc}"})
            if not wiki_enabled:
                return _tool_result({
                    "status": "ERROR",
                    "error": "Wiki decision publishing is disabled for this profile.",
                })
        try:
            expiry_days = max(1, min(365, int(ctx.get_config("default_expiry_days", default=14))))
        except (TypeError, ValueError):
            return _tool_result({"status": "ERROR", "error": "default_expiry_days must be an integer."})
        try:
            auto_resume = False if weekly else _config_bool(
                ctx.get_config("auto_resume", default=True), True
            )
        except ValueError as exc:
            return _tool_result({"status": "ERROR", "error": f"Decision Inbox configuration error: {exc}"})

        session_id = str(kwargs.get("session_id") or "").strip()
        task_id = str(kwargs.get("task_id") or "").strip()
        if not session_id:
            return _tool_result({
                "status": "ERROR",
                "error": "No recoverable Hermes session ID is available; ask in ordinary chat instead.",
            })
        if _session_is_delegated(session_id):
            return _tool_result({
                "status": "ERROR",
                "error": "Delegated agents cannot notify the client; return this decision to the parent agent.",
            })

        payload = request.model_dump(mode="json", exclude_none=True)
        payload.update({
            "source_profile": profile,
            "source_session_id": session_id,
            "source_task_id": task_id or session_id,
            "plugin_version": __version__,
            "auto_resume": auto_resume,
            "default_expiry_days": expiry_days,
        })
        try:
            result = DecisionInboxClient(service_url, token).publish(payload)
        except DecisionInboxError as exc:
            return _tool_result({"status": "ERROR", "error": str(exc)})
        return _tool_result({
            "status": "PUBLISHED",
            "decision_id": result["decision_id"],
            "message": (
                "Weekly review published. Return [SILENT]."
                if weekly else "Decision published. Return [SILENT] until the user responds."
            ),
            "deduplicated": bool(result.get("deduplicated")),
        })

    return handle


def build_handler(ctx):
    """Build the weekly-only Wiki review publisher."""
    return _build_publisher(ctx, WeeklyWikiReviewRequest, weekly=True)


def build_general_handler(ctx):
    """Build the general session-bound decision publisher."""
    return _build_publisher(ctx, SessionDecisionRequest, weekly=False)


def build_read_handler(ctx):
    """Build a read-only Decision Inbox query scoped to the current profile."""
    def handle(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            request = ReadDecisionsRequest.model_validate(args or {})
        except Exception as exc:
            return _tool_result({"status": "ERROR", "error": f"Invalid Decision Inbox read request: {exc}"})

        service_url = str(ctx.get_config("service_url", default="http://decision-inbox:8080") or "").strip()
        token = _secret("DECISION_INBOX_PUBLISH_TOKEN")
        if not service_url or not token:
            return _tool_result({
                "status": "ERROR",
                "error": "Decision Inbox is not configured; ask in ordinary chat instead.",
            })
        session_id = str(kwargs.get("session_id") or "").strip()
        if request.scope == "session" and not session_id:
            return _tool_result({
                "status": "ERROR",
                "error": "No recoverable Hermes session ID is available for a session-scoped read.",
            })
        try:
            result = DecisionInboxClient(service_url, token).read_decisions(
                scope=request.scope,
                source_session_id=session_id if request.scope == "session" else None,
                include_resolved=request.include_resolved,
                limit=request.limit,
            )
        except DecisionInboxError as exc:
            return _tool_result({"status": "ERROR", "error": str(exc)})
        return _tool_result({
            "status": "OK",
            "scope": request.scope,
            "include_resolved": request.include_resolved,
            "items": result["items"],
        })

    return handle


def check_available(ctx) -> bool:
    return bool(
        str(ctx.get_config("service_url", default="") or "").strip()
        and _secret("DECISION_INBOX_PUBLISH_TOKEN")
    )


__all__ = [
    "READ_DECISIONS_SCHEMA",
    "REQUEST_DECISION_SCHEMA",
    "SESSION_DECISION_SCHEMA",
    "build_general_handler",
    "build_handler",
    "build_read_handler",
    "check_available",
]
