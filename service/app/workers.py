"""Single-process durable outbox and execution workers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .config import Settings
from .database import Database
from .integrations import HermesClient, TelegramSender, decision_resume_prompt
from .wiki import WikiExecutor

logger = logging.getLogger(__name__)


class Workers:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.telegram = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id, settings.mini_app_url)
        self.hermes = HermesClient(settings.hermes_api_url, settings.hermes_profile_api_keys)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            self.db.expire_due_decisions()
            did_work = await self._notification_once()
            did_work = await self._execution_once() or did_work
            if not did_work:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()

    async def _notification_once(self) -> bool:
        item = self.db.claim_notification()
        if not item:
            return False
        try:
            payload = json.loads(item["payload_json"])
            await self.telegram.send(payload["text"], payload.get("button"))
        except Exception as exc:
            self.db.finish_notification(item["notification_id"], False, str(exc))
        else:
            self.db.finish_notification(item["notification_id"], True)
        return True

    async def _execution_once(self) -> bool:
        item = self.db.claim_execution()
        if not item:
            return False
        try:
            manifest = json.loads(item["manifest_json"])
            if item["kind"] == "resume":
                prompt = decision_resume_prompt(manifest)
                await self.hermes.require_session(item["source_profile"], item["source_session_id"])
                run_id = await self.hermes.start_run(
                    item["source_profile"], prompt, item["idempotency_key"], item["source_session_id"])
            elif item["kind"] == "wiki_apply":
                approved = [r["card_id"] for r in manifest["responses"]
                            if r["outcome"] in {"recommended", "alternative"}
                            and r["execution_kind"] == "wiki_patch_v1"]
                executor = WikiExecutor(
                    self.settings.wiki_root,
                    self.settings.backup_root,
                    self.settings.manifest_root / "receipts",
                )
                result = await asyncio.to_thread(
                    executor.apply_manifest,
                    Path(item["manifest_path"]),
                    item["sha256"],
                    approved,
                )
                self.db.finish_execution(item["execution_id"], True, result)
                self.db.archive_decision(item["decision_id"], "wiki-apply-worker")
                self.db.enqueue_notification(
                    item["decision_id"],
                    "execution_complete",
                    f"Wiki update complete\n{len(approved)} applied · 0 failed",
                    "Open Wiki Review",
                )
                return True
            else:
                raise RuntimeError(f"unsupported execution kind: {item['kind']}")
            self.db.set_execution_run_id(item["execution_id"], run_id)
            result = await self.hermes.wait_run(item["source_profile"] if item["kind"] == "resume" else "default", run_id)
            success = str(result.get("status") or "").lower() == "completed"
            self.db.finish_execution(item["execution_id"], success, result,
                                     None if success else str(result.get("error") or "Hermes run blocked"))
            if success:
                message = (
                    "Wiki update complete\nOpen the Decision Inbox to view the receipt."
                    if item["kind"] == "wiki_apply"
                    else f"{item['source_profile'].title()} finished after your decision."
                )
                self.db.enqueue_notification(item["decision_id"], "execution_complete", message, "Open Decision Inbox")
            else:
                self.db.enqueue_notification(item["decision_id"], "execution_blocked",
                                             "Decision follow-up is blocked. Open Hermes for details.")
        except Exception as exc:
            logger.exception("decision execution failed")
            self.db.finish_execution(item["execution_id"], False, {}, str(exc))
            try:
                self.db.enqueue_notification(item["decision_id"], "execution_blocked",
                                             "Decision follow-up is blocked. Open Hermes for details.")
            except Exception:
                logger.exception("blocker notification failed")
        return True
