"""Hermes-side publication guard and durable conversation continuation receiver."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import DecisionInboxClient, DecisionInboxError
from .continuation import CONTINUATION_MARKER, decision_continuation_prompt
from .tool import _secret


logger = logging.getLogger(__name__)


class PublicationGuard:
    """Guarantee a visible direct link after an interactive publication."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, publication: dict[str, Any]) -> None:
        with self._lock:
            self._pending[session_id] = dict(publication)

    def transform(self, response_text: str = "", session_id: str = "", **_: Any) -> str | None:
        with self._lock:
            publication = self._pending.pop(session_id, None)
        if not publication:
            return None
        url = str(publication.get("decision_url") or "").strip()
        decision_id = str(publication.get("decision_id") or "").strip()
        target = f"[Open Decision Inbox]({url})" if url else f"Open Decision Inbox ({decision_id})"
        notice = f"I need your decision: {target}. I’ll continue here after you apply it."
        current = str(response_text or "").strip()
        if url and url in current:
            return None
        if not current or current == "[SILENT]":
            return notice
        return f"{current}\n\n{notice}"


@dataclass(frozen=True)
class InflightContinuation:
    execution_id: str
    lease_token: str
    target_session_id: str
    source_profile: str


class ContinuationReceiver:
    """Claim service-owned continuations and inject only into the verified source route."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.profile = getattr(ctx, "profile_name", None) or "default"
        self.poll_seconds = self._poll_seconds()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._active_cli_sessions: dict[str, set[str]] = {}
        self._inflight: dict[str, InflightContinuation] = {}
        self._publish_tokens: dict[str, str] = {}
        self._token_lock = threading.Lock()

    def _poll_seconds(self) -> float:
        try:
            value = float(self._get_config("continuation_poll_seconds", 2.0))
        except (TypeError, ValueError):
            return 2.0
        return max(0.5, min(value, 30.0))

    def _enabled(self, profile: str | None = None) -> bool:
        value = self._get_config("session_delivery_enabled", True, profile=profile)
        return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _get_config(self, key: str, default: Any, *, profile: str | None = None) -> Any:
        getter = getattr(self.ctx, "get_config", None)
        if not getter:
            return default
        with self._profile_scope(profile or self.profile):
            return getter(key, default=default)

    def _client(self, profile: str | None = None) -> DecisionInboxClient:
        selected = profile or self.profile
        service_url = str(self._get_config(
            "service_url", "http://decision-inbox:8080", profile=selected,
        ) or "").strip()
        with self._token_lock:
            token = self._publish_tokens.get(selected, "")
        if not token:
            self.capture_secret(selected)
            with self._token_lock:
                token = self._publish_tokens.get(selected, "")
        if not service_url or not token:
            raise DecisionInboxError("Decision Inbox continuation receiver is not configured")
        return DecisionInboxClient(service_url, token)

    @contextmanager
    def _profile_scope(self, profile: str):
        manager = getattr(self.ctx, "_manager", None)
        if manager is None:
            yield
            return
        try:
            from hermes_cli.profiles import get_profile_dir
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override
            profile_home = get_profile_dir(profile)
            token = set_hermes_home_override(str(profile_home))
        except Exception:
            yield
            return
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    def set_publish_token(self, profile: str, token: str) -> None:
        clean = str(token or "").strip()
        if clean:
            with self._token_lock:
                self._publish_tokens[profile or "default"] = clean

    def capture_secret(self, profile: str | None = None) -> None:
        selected = profile or getattr(self.ctx, "profile_name", None) or self.profile
        try:
            with self._profile_scope(selected):
                self.set_publish_token(selected, _secret("DECISION_INBOX_PUBLISH_TOKEN"))
        except Exception:
            logger.debug(
                "No secret scope is active for Decision Inbox profile %s", selected,
            )

    def capture_configured_profiles(self) -> None:
        """Load each enabled profile's token without sharing it across profile requests."""
        manager = getattr(self.ctx, "_manager", None)
        root = Path(getattr(manager, "home_path", "")) if manager is not None else None
        profiles = [self.profile]
        if root and root.is_dir():
            if root.name == "profiles":
                root = root.parent
            profiles = ["default", *sorted(
                path.name for path in (root / "profiles").iterdir()
                if path.is_dir() and (path / "config.yaml").is_file()
            )] if (root / "profiles").is_dir() else [self.profile]
        for profile in dict.fromkeys(profiles):
            try:
                with self._profile_scope(profile):
                    from agent.secret_scope import build_profile_secret_scope
                    from hermes_cli.config import load_config_readonly
                    from hermes_cli.profiles import get_profile_dir
                    config = load_config_readonly() or {}
                    plugins = config.get("plugins") or {}
                    enabled = set(plugins.get("enabled") or [])
                    disabled = set(plugins.get("disabled") or [])
                    secrets = build_profile_secret_scope(get_profile_dir(profile))
                plugin_id = getattr(self.ctx, "plugin_id", "hermes-decision-inbox")
                if plugin_id not in enabled or plugin_id in disabled:
                    continue
                self.set_publish_token(
                    profile, str(secrets.get("DECISION_INBOX_PUBLISH_TOKEN") or ""),
                )
            except Exception:
                if profile != self.profile:
                    continue
                self.capture_secret(profile)

    def ensure_started(self) -> None:
        self.capture_configured_profiles()
        if self._stopping or (self._task and not self._task.done()):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = self.ctx.spawn_task(
            self.run(), name=f"hermes-decision-inbox:{self.profile}:continuations",
        )

    def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()

    def session_started(self, session_id: str = "", **_: Any) -> None:
        profile = getattr(self.ctx, "profile_name", None) or self.profile
        self.capture_secret(profile)
        if session_id:
            self._active_cli_sessions.setdefault(profile, set()).add(session_id)
        self.ensure_started()

    def session_reset(self, session_id: str = "", **_: Any) -> None:
        profile = getattr(self.ctx, "profile_name", None) or self.profile
        self._active_cli_sessions.setdefault(profile, set()).discard(session_id)

    async def run(self) -> None:
        while not self._stopping:
            with self._token_lock:
                profiles = sorted(self._publish_tokens)
            for profile in profiles:
                if not self._enabled(profile):
                    continue
                try:
                    await self.poll_once(profile)
                except asyncio.CancelledError:
                    raise
                except DecisionInboxError as exc:
                    logger.debug(
                        "Decision Inbox continuation receiver is waiting for %s: %s", profile, exc,
                    )
                except Exception:
                    logger.exception(
                        "Decision Inbox continuation poll failed for profile %s", profile,
                    )
            await asyncio.sleep(self.poll_seconds)

    async def poll_once(self, profile: str | None = None) -> bool:
        selected = profile or self.profile
        client = self._client(selected)
        response = await asyncio.to_thread(client.pending_continuations, 10, selected)
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise DecisionInboxError("Decision service returned an invalid continuation list")
        did_work = False
        for item in items:
            route = self._route_target(item, selected)
            if not route:
                if item.get("status") == "QUEUED":
                    await asyncio.to_thread(
                        client.wait_continuation,
                        str(item["execution_id"]),
                        "The exact originating session is not currently active.",
                    )
                    did_work = True
                continue
            if item.get("status") == "DISPATCHED":
                recovered = self._recorded_completion(
                    route, str(item["execution_id"]), selected,
                )
                if recovered and recovered.get("assistant_response"):
                    await asyncio.to_thread(
                        client.complete_continuation,
                        str(item["execution_id"]),
                        str(item.get("lease_token") or ""),
                        recovered,
                    )
                    did_work = True
                elif recovered is None:
                    await asyncio.to_thread(
                        client.fail_continuation,
                        str(item["execution_id"]),
                        str(item.get("lease_token") or ""),
                        "The dispatched continuation marker was not recorded before its lease expired.",
                        retryable=True,
                    )
                    did_work = True
                continue
            claimed = await asyncio.to_thread(
                client.claim_continuation,
                str(item["execution_id"]),
                f"{selected}:{os.getpid()}",
                90,
            )
            lease_token = str(claimed["lease_token"])
            prompt = decision_continuation_prompt(claimed["manifest"], str(item["execution_id"]))
            session_key = str(claimed.get("source_session_key") or "").strip() or None
            verified_route = self._route_target(claimed, selected)
            if not verified_route or verified_route != route:
                await asyncio.to_thread(
                    client.fail_continuation, str(item["execution_id"]), lease_token,
                    "The saved conversation route changed before injection.", retryable=True,
                )
                did_work = True
                continue
            await asyncio.to_thread(
                client.dispatch_continuation, str(item["execution_id"]), lease_token,
            )
            self._inflight[str(item["execution_id"])] = InflightContinuation(
                execution_id=str(item["execution_id"]),
                lease_token=lease_token,
                target_session_id=verified_route,
                source_profile=selected,
            )
            try:
                with self._profile_scope(selected):
                    accepted = bool(self.ctx.inject_message(prompt, session_key=session_key))
            except Exception as exc:
                self._inflight.pop(str(item["execution_id"]), None)
                await asyncio.to_thread(
                    client.fail_continuation, str(item["execution_id"]), lease_token,
                    f"Hermes injection failed: {exc}", retryable=True,
                )
                did_work = True
                continue
            if not accepted:
                self._inflight.pop(str(item["execution_id"]), None)
                await asyncio.to_thread(
                    client.fail_continuation, str(item["execution_id"]), lease_token,
                    "Hermes did not accept the saved conversation route.", retryable=True,
                )
                did_work = True
                continue
            did_work = True
        return did_work

    def _route_target(self, item: dict[str, Any], profile: str | None = None) -> str | None:
        selected = profile or self.profile
        if item.get("source_profile") and str(item["source_profile"]) != selected:
            return None
        original = str(item.get("source_session_id") or "").strip()
        if not original:
            return None
        try:
            with self._profile_scope(selected):
                from hermes_state import SessionDB
                with SessionDB(read_only=True) as db:
                    original_row = db.get_session(original)
                    if not original_row:
                        return None
                    tip = db.get_compression_tip(original) or original
                    target = db.get_session(tip)
        except Exception:
            logger.exception("Could not verify Hermes continuation route")
            return None
        if not target or target.get("ended_at") is not None:
            return None
        saved_key = str(item.get("source_session_key") or "").strip()
        saved_surface = str(item.get("source_surface") or "").strip().lower()
        if saved_key:
            if str(target.get("session_key") or "").strip() != saved_key:
                return None
            if saved_surface and str(target.get("source") or "").strip().lower() != saved_surface:
                return None
            return tip
        if saved_surface not in {"cli", "tui"}:
            return None
        active = self._active_cli_sessions.get(selected, set())
        return tip if tip in active or original in active else None

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or "") for item in content if isinstance(item, dict)
            )
        return str(content or "")

    def _recorded_completion(
        self, session_id: str, execution_id: str, profile: str | None = None,
    ) -> dict[str, Any] | None:
        marker = f"{CONTINUATION_MARKER}: {execution_id}"
        try:
            with self._profile_scope(profile or self.profile):
                from hermes_state import SessionDB
                with SessionDB(read_only=True) as db:
                    messages = db.get_messages(session_id, include_inactive=True, include_compacted=True)
        except Exception:
            logger.exception("Could not reconcile dispatched Decision Inbox continuation")
            return None
        seen = False
        for message in messages:
            text = self._message_text(message)
            if message.get("role") == "user" and marker in text:
                seen = True
                continue
            if seen and message.get("role") == "assistant" and text.strip():
                return {"assistant_response": text, "recovered_from_session": True}
        return {"marker_recorded": True} if seen else None

    def post_llm_call(
        self, session_id: str = "", user_message: str = "", assistant_response: str = "", **_: Any,
    ) -> None:
        self.ensure_started()
        marker = f"{CONTINUATION_MARKER}: "
        marker_line = next(
            (line for line in str(user_message or "").splitlines() if line.startswith(marker)), None,
        )
        if not marker_line:
            return
        execution_id = marker_line.removeprefix(marker).strip()
        inflight = self._inflight.get(execution_id)
        profile = getattr(self.ctx, "profile_name", None) or self.profile
        if (
            not inflight
            or inflight.target_session_id != session_id
            or inflight.source_profile != profile
            or not str(assistant_response or "").strip()
        ):
            return

        async def acknowledge() -> None:
            try:
                await asyncio.to_thread(
                    self._client(inflight.source_profile).complete_continuation,
                    execution_id,
                    inflight.lease_token,
                    {"assistant_response": assistant_response, "source_session_id": session_id},
                )
            except Exception:
                logger.exception("Could not acknowledge Decision Inbox continuation completion")
            else:
                self._inflight.pop(execution_id, None)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No event loop was available to acknowledge continuation %s", execution_id)
            return
        self.ctx.spawn_task(acknowledge(), name=f"decision-continuation-complete:{execution_id}")


__all__ = ["ContinuationReceiver", "InflightContinuation", "PublicationGuard"]
