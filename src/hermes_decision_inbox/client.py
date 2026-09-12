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

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.service_url.rstrip('/')}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.publish_token}",
            "User-Agent": "hermes-decision-inbox/0.3.0",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
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
        if not isinstance(result, dict):
            raise DecisionInboxError("Decision service returned an invalid response")
        return result

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/internal/v1/decisions", payload)
        if not isinstance(result, dict) or not result.get("decision_id"):
            raise DecisionInboxError("Decision service did not confirm durable publication")
        return result

    def pending_continuations(self, limit: int = 10, source_profile: str | None = None) -> dict[str, Any]:
        query = {"limit": str(max(1, min(limit, 25)))}
        if source_profile:
            query["source_profile"] = source_profile
        return self._request("GET", f"/internal/v1/continuations?{urlencode(query)}")

    def claim_continuation(
        self, execution_id: str, consumer_id: str, lease_seconds: int = 90,
    ) -> dict[str, Any]:
        return self._request("POST", f"/internal/v1/continuations/{execution_id}/claim", {
            "consumer_id": consumer_id,
            "lease_seconds": lease_seconds,
        })

    def dispatch_continuation(self, execution_id: str, lease_token: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/v1/continuations/{execution_id}/dispatch", {
            "lease_token": lease_token,
        })

    def complete_continuation(
        self, execution_id: str, lease_token: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request("POST", f"/internal/v1/continuations/{execution_id}/complete", {
            "lease_token": lease_token,
            "result": result,
        })

    def fail_continuation(
        self, execution_id: str, lease_token: str, error: str, *, retryable: bool,
    ) -> dict[str, Any]:
        return self._request("POST", f"/internal/v1/continuations/{execution_id}/failure", {
            "lease_token": lease_token,
            "error": error,
            "retryable": retryable,
        })

    def wait_continuation(self, execution_id: str, reason: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/v1/continuations/{execution_id}/waiting", {
            "reason": reason,
        })

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
                "User-Agent": "hermes-decision-inbox/0.3.0",
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
