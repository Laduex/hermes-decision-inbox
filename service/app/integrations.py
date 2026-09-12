"""Outbound Telegram and Hermes API clients."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

import httpx

from hermes_decision_inbox.continuation import decision_continuation_prompt


class TelegramSender:
    def __init__(self, token: str, chat_id: str, mini_app_url: str):
        self.token = token
        self.chat_id = chat_id
        self.mini_app_url = mini_app_url

    async def send(
        self, text: str, button: str | None = None, button_url: str | None = None,
    ) -> dict[str, Any]:
        if not self.token or not self.chat_id:
            raise RuntimeError("Telegram delivery is not configured")
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{
                "text": button, "url": self.mini_app_url,
            }]]}
            payload["reply_markup"]["inline_keyboard"][0][0]["url"] = button_url or self.mini_app_url
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload)
            response.raise_for_status()
            data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram rejected the notification")
        return data["result"]


class HermesClient:
    def __init__(self, base_url: str, profile_keys: dict[str, str]):
        self.base_url = base_url.rstrip("/")
        self.profile_keys = profile_keys

    def _key(self, profile: str) -> str:
        key = self.profile_keys.get(profile) or self.profile_keys.get("default") or self.profile_keys.get("*")
        if not key:
            raise RuntimeError(f"No Hermes API credential is configured for profile {profile}")
        return key

    def _url(self, profile: str, path: str) -> str:
        prefix = "" if profile in {"", "default"} else f"/p/{quote(profile, safe='')}"
        return f"{self.base_url}{prefix}{path}"

    async def start_run(self, profile: str, input_text: str, idempotency_key: str,
                        session_id: str | None = None) -> str:
        body: dict[str, Any] = {"input": input_text}
        if session_id:
            body["session_id"] = session_id
        headers = {
            "Authorization": f"Bearer {self._key(profile)}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            deadline = asyncio.get_running_loop().time() + 600
            while True:
                response = await client.post(self._url(profile, "/v1/runs"), headers=headers, json=body)
                if response.status_code != 429 or asyncio.get_running_loop().time() >= deadline:
                    response.raise_for_status()
                    data = response.json()
                    break
                retry_after = min(15.0, max(1.0, float(response.headers.get("Retry-After", "2"))))
                await asyncio.sleep(retry_after)
        run_id = data.get("id") or data.get("run_id")
        if not run_id:
            raise RuntimeError("Hermes did not return a run ID")
        return str(run_id)

    async def require_session(self, profile: str, session_id: str) -> None:
        headers = {"Authorization": f"Bearer {self._key(profile)}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                self._url(profile, f"/api/sessions/{quote(session_id, safe='')}"), headers=headers,
            )
        if response.status_code == 404:
            raise RuntimeError("The original Hermes session cannot be recovered")
        response.raise_for_status()

    async def wait_run(self, profile: str, run_id: str, timeout_seconds: int = 1800) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        headers = {"Authorization": f"Bearer {self._key(profile)}"}
        async with httpx.AsyncClient(timeout=20) as client:
            while asyncio.get_running_loop().time() < deadline:
                response = await client.get(
                    self._url(profile, f"/v1/runs/{quote(run_id, safe='')}"), headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                status = str(data.get("status") or "").lower()
                if status in {"completed", "failed", "cancelled", "blocked", "error"}:
                    return data
                await asyncio.sleep(2)
        raise RuntimeError("Hermes run did not finish before the monitoring deadline")


def decision_resume_prompt(manifest: dict[str, Any]) -> str:
    """Backward-compatible name for the exact-session Runs API fallback envelope."""
    return decision_continuation_prompt(manifest)


def wiki_apply_prompt(execution: dict[str, Any], approved: list[str], backup_root: str,
                      receipt_root: str) -> str:
    return "\n".join([
        "Use the `hermes-decision-inbox:memory-wiki-decision-applier` skill.",
        f"Immutable manifest path: {execution['manifest_path']}",
        f"Manifest SHA-256: {execution['sha256']}",
        f"Approved card IDs: {json.dumps(approved)}",
        f"Backup path: {backup_root}",
        f"Receipt destination: {receipt_root}",
        "Apply only the approved wiki_patch_v1 cards. Fail closed on any conflict.",
    ])
