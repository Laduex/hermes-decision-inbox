"""Hermes-side publication guard and durable conversation continuation receiver."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
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


class ContinuationReceiver:
    """Claim service-owned continuations and inject only into the verified source route."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.profile = getattr(ctx, "profile_name", None) or "default"
        self.consumer_id = f"{self.profile}:{os.getpid()}"
        self.poll_seconds = self._poll_seconds()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._active_cli_sessions: set[str] = set()
        self._inflight: dict[str, InflightContinuation] = {}
        self._publish_token = ""

    def _poll_seconds(self) -> float:
        try:
            value = float(self._get_config("continuation_poll_seconds", 2.0))
        except (TypeError, ValueError):
            return 2.0
        return max(0.5, min(value, 30.0))

    def _enabled(self) -> bool:
        value = self._get_config("session_delivery_enabled", True)
        return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _get_config(self, key: str, default: Any) -> Any:
        getter = getattr(self.ctx, "get_config", None)
        return getter(key, default=default) if getter else default

    def _client(self) -> DecisionInboxClient:
        service_url = str(self._get_config("service_url", "http://decision-inbox:8080") or "").strip()
        token = self._publish_token
        if not token:
            self.capture_secret()
            token = self._publish_token
        if not service_url or not token:
            raise DecisionInboxError("Decision Inbox continuation receiver is not configured")
        return DecisionInboxClient(service_url, token)

    def set_publish_token(self, token: str) -> None:
        self._publish_token = str(token or "").strip()

    def capture_secret(self) -> None:
        try:
            self.set_publish_token(_secret("DECISION_INBOX_PUBLISH_TOKEN"))
        except Exception:
            logger.debug("No profile secret scope is active for the continuation receiver")

    def ensure_started(self) -> None:
        if self._stopping or not self._enabled() or (self._task and not self._task.done()):
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
        self.capture_secret()
        if session_id:
            self._active_cli_sessions.add(session_id)
        self.ensure_started()

    def session_reset(self, session_id: str = "", **_: Any) -> None:
        self._active_cli_sessions.discard(session_id)

    async def run(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except DecisionInboxError as exc:
                logger.debug("Decision Inbox continuation receiver is waiting: %s", exc)
            except Exception:
                logger.exception("Decision Inbox continuation poll failed")
            await asyncio.sleep(self.poll_seconds)

    async def poll_once(self) -> bool:
        client = self._client()
        response = await asyncio.to_thread(client.pending_continuations, 10, self.profile)
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise DecisionInboxError("Decision service returned an invalid continuation list")
        did_work = False
        for item in items:
            route = self._route_target(item)
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
                recovered = self._recorded_completion(route, str(item["execution_id"]))
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
                self.consumer_id,
                90,
            )
            lease_token = str(claimed["lease_token"])
            prompt = decision_continuation_prompt(claimed["manifest"], str(item["execution_id"]))
            session_key = str(claimed.get("source_session_key") or "").strip() or None
            verified_route = self._route_target(claimed)
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
            )
            try:
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

    def _route_target(self, item: dict[str, Any]) -> str | None:
        original = str(item.get("source_session_id") or "").strip()
        if not original:
            return None
        try:
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
        return tip if tip in self._active_cli_sessions or original in self._active_cli_sessions else None

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

    def _recorded_completion(self, session_id: str, execution_id: str) -> dict[str, Any] | None:
        marker = f"{CONTINUATION_MARKER}: {execution_id}"
        try:
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
        if not inflight or inflight.target_session_id != session_id or not str(assistant_response or "").strip():
            return

        async def acknowledge() -> None:
            try:
                await asyncio.to_thread(
                    self._client().complete_continuation,
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
