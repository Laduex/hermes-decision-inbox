"""Small synchronous client used inside Hermes tool dispatch."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode


class DecisionInboxError(RuntimeError):
    """A truthful, user-actionable publishing failure."""


@dataclass(frozen=True)
class DecisionInboxClient:
    service_url: str
    publish_token: str
    timeout: float = 10.0

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.service_url.rstrip('/')}/internal/v1/decisions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.publish_token}",
                "Content-Type": "application/json",
                "User-Agent": "hermes-decision-inbox/0.2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise DecisionInboxError(f"Decision service rejected the request ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DecisionInboxError(f"Decision service is unavailable: {exc}") from exc
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DecisionInboxError("Decision service returned invalid JSON") from exc
        if not isinstance(result, dict) or not result.get("decision_id"):
            raise DecisionInboxError("Decision service did not confirm durable publication")
        return result

    def status(self, decision_id: str) -> dict[str, Any]:
        url = f"{self.service_url.rstrip('/')}/internal/v1/decisions/{decision_id}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.publish_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise DecisionInboxError(f"Could not read decision status: {exc}") from exc

    def read_decisions(
        self,
        *,
        scope: str,
        source_session_id: str | None = None,
        include_resolved: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = {"scope": scope, "include_resolved": str(include_resolved).lower(), "limit": str(limit)}
        if source_session_id:
            query["source_session_id"] = source_session_id
        url = f"{self.service_url.rstrip('/')}/internal/v1/decisions?{urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.publish_token}",
                "User-Agent": "hermes-decision-inbox/0.2.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise DecisionInboxError(f"Could not read Decision Inbox entries: {exc}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise DecisionInboxError("Decision service returned an invalid decision list")
        return result
