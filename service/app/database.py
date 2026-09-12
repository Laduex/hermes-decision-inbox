"""Transactional SQLite repository for decisions and audit history."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from hermes_decision_inbox.schemas import DecisionRequest


OPEN_STATES = {
    "DRAFT", "READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT",
    "SUBMITTED", "QUEUED_FOR_RESUME", "RESUMING", "READY_TO_APPLY", "APPLYING", "BLOCKED",
}
COMPLETED_STATES = {"COMPLETED", "EXPIRED", "CANCELLED"}
HIDDEN_STATES = {"ARCHIVED"}
EXPIRABLE_STATES = {"DRAFT", "READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT"}
MERGEABLE_STATES = {"READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT"}
ARCHIVEABLE_STATES = {"DRAFT", "READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT"}
# Kept for the legacy discard endpoint and existing audit history. New UI flows use archive semantics.
DISCARDABLE_STATES = ARCHIVEABLE_STATES


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


class StateError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path, manifest_root: Path, default_expiry_days: int = 14,
                 auto_resume: bool = True, weekly_only: bool = False,
                 mini_app_url: str = ""):
        self.path = Path(path)
        self.manifest_root = Path(manifest_root)
        self.default_expiry_days = default_expiry_days
        self.auto_resume = auto_resume
        self.weekly_only = weekly_only
        self.mini_app_url = mini_app_url.rstrip("?")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        migration_root = Path(__file__).resolve().parents[1] / "migrations"
        with self.connect() as conn:
            conn.executescript((migration_root / "001_initial.sql").read_text(encoding="utf-8"))
            decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_requests)")}
            if "auto_resume" not in decision_columns:
                conn.execute("ALTER TABLE decision_requests ADD COLUMN auto_resume INTEGER NOT NULL DEFAULT 1")
            option_columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_options)")}
            if "execution_payload_json" not in option_columns:
                conn.executescript((migration_root / "002_tailscale_weekly.sql").read_text(encoding="utf-8"))
            decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_requests)")}
            for column, definition in (
                ("source_surface", "TEXT"),
                ("source_session_key", "TEXT"),
            ):
                if column not in decision_columns:
                    conn.execute(f"ALTER TABLE decision_requests ADD COLUMN {column} {definition}")
            execution_columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_attempts)")}
            for column, definition in (
                ("lease_token", "TEXT"),
                ("lease_expires_at", "TEXT"),
                ("consumer_id", "TEXT"),
                ("next_attempt_at", "TEXT"),
            ):
                if column not in execution_columns:
                    conn.execute(f"ALTER TABLE execution_attempts ADD COLUMN {column} {definition}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_stream_status "
                "ON decision_requests(source_profile,source_session_id,name,status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_delivery_queue "
                "ON execution_attempts(kind,status,next_attempt_at,created_at)"
            )
            conn.execute("UPDATE notification_outbox SET status='RETRY' WHERE status='SENDING'")
            conn.execute("UPDATE execution_attempts SET status='QUEUED' WHERE status='RUNNING'")
            conn.execute(
                "UPDATE execution_attempts SET status='QUEUED',lease_token=NULL,lease_expires_at=NULL,consumer_id=NULL "
                "WHERE kind='conversation_resume' AND status='CLAIMED' AND lease_expires_at<=?",
                (iso(),),
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if candidate.exists():
                os.chmod(candidate, 0o600)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _audit(self, conn: sqlite3.Connection, decision_id: str, event: str, actor: str,
               old: str | None, new: str | None, payload: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_events(decision_id,actor,event_type,from_status,to_status,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (decision_id, actor, event, old, new, canonical_json(payload or {}), iso()),
        )

    def decision_url(self, decision_id: str) -> str:
        if not self.mini_app_url:
            return ""
        parts = urlsplit(self.mini_app_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["decision"] = decision_id
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    @staticmethod
    def delivery_mode(source_surface: str | None, source_session_key: str | None) -> str:
        surface = (source_surface or "").strip().lower()
        if surface in {"cron", "api", "api-server", "runs-api"}:
            return "session_api"
        if source_session_key or surface in {"cli", "tui"}:
            return "conversation"
        return "session_api"

    def publish(self, raw: dict[str, Any], credential_profile: str) -> dict[str, Any]:
        source_profile = str(raw.get("source_profile") or "").strip()
        if credential_profile != "*" and source_profile != credential_profile:
            raise PermissionError("publisher credential does not match source_profile")
        source_session = str(raw.get("source_session_id") or "").strip()
        source_task = str(raw.get("source_task_id") or source_session).strip()
        source_surface = str(raw.get("source_surface") or "").strip() or None
        source_session_key = str(raw.get("source_session_key") or "").strip() or None
        if not source_profile or not source_session:
            raise ValueError("source_profile and source_session_id are required")
        parsed = DecisionRequest.model_validate({
            key: value for key, value in raw.items()
            if key not in {
                "source_profile", "source_session_id", "source_task_id", "plugin_version",
                "source_surface", "source_session_key", "auto_resume", "default_expiry_days",
            }
        })
        cards = parsed.cards()
        if self.weekly_only and (not parsed.batch or any(card.execution is None for card in cards)):
            raise ValueError("only batched Weekly Memory Wiki reviews may be published")
        payload_hash = hashlib.sha256(canonical_json(parsed.model_dump(mode="json", exclude_none=True)).encode()).hexdigest()
        created = now()
        expiry_days = max(1, min(365, int(raw.get("default_expiry_days", self.default_expiry_days))))
        request_auto_resume = bool(raw.get("auto_resume", self.auto_resume))
        expires = created + timedelta(days=expiry_days)
        with self.transaction() as conn:
            duplicate = conn.execute(
                "SELECT decision_id FROM decision_request_dedupes WHERE source_profile=? AND source_session_id=? "
                "AND dedupe_hash=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (source_profile, source_session, payload_hash, iso(created - timedelta(minutes=5))),
            ).fetchone()
            if duplicate:
                conn.execute(
                    "UPDATE decision_requests SET source_surface=COALESCE(source_surface,?),"
                    "source_session_key=COALESCE(source_session_key,?) WHERE decision_id=?",
                    (source_surface, source_session_key, duplicate["decision_id"]),
                )
                return self._publication_result(
                    duplicate["decision_id"], source_surface, source_session_key, True, False,
                )

            existing = conn.execute(
                "SELECT decision_id,status,decision_type FROM decision_requests WHERE source_profile=? AND source_session_id=? "
                "AND name=? "
                "AND status IN (%s) ORDER BY created_at DESC LIMIT 1" % ",".join("?" for _ in OPEN_STATES),
                (source_profile, source_session, parsed.name, *sorted(OPEN_STATES)),
            ).fetchone()
            decision_id = existing["decision_id"] if existing else _id("dec")
            decision_type = "wiki_review" if any(card.execution for card in cards) else "ordinary"
            title = parsed.title or parsed.name.replace("-", " ").title()
            if existing:
                if existing["status"] not in MERGEABLE_STATES:
                    raise ConflictError(
                        f"session already has in-flight decision {decision_id} in {existing['status']}"
                    )
                if existing["decision_type"] != decision_type:
                    raise ConflictError(
                        f"session decision {decision_id} cannot mix {existing['decision_type']} and {decision_type} cards"
                    )
                start = conn.execute(
                    "SELECT COALESCE(MAX(position),0) AS p FROM decision_cards WHERE decision_id=?", (decision_id,),
                ).fetchone()["p"]
                conn.execute(
                    "UPDATE decision_requests SET updated_at=?,version=version+1,status='READY',"
                    "source_surface=COALESCE(source_surface,?),source_session_key=COALESCE(source_session_key,?) "
                    "WHERE decision_id=?",
                    (iso(created), source_surface, source_session_key, decision_id),
                )
                old_status = existing["status"]
                merged = True
            else:
                start = 0
                conn.execute(
                    "INSERT INTO decision_requests("
                    "decision_id,name,title,source_profile,source_session_id,source_task_id,plugin_version,priority,"
                    "status,decision_type,dedupe_hash,auto_resume,version,created_at,updated_at,expires_at,"
                    "source_surface,source_session_key"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, parsed.name, title, source_profile, source_session, source_task,
                     str(raw.get("plugin_version") or "unknown"), parsed.priority, "READY", decision_type,
                     payload_hash, 1 if request_auto_resume else 0, 1, iso(created), iso(created), iso(expires),
                     source_surface, source_session_key),
                )
                old_status = None
                merged = False
            self._insert_cards(conn, decision_id, cards, source_profile, source_session, start, created, expires)
            conn.execute(
                "INSERT INTO decision_request_dedupes VALUES(?,?,?,?,?)",
                (source_profile, source_session, payload_hash, decision_id, iso(created)),
            )
            message = (
                f"Your weekly Memory Wiki review is ready — {len(cards)} cards"
                if decision_type == "wiki_review"
                else f"{source_profile.title()} needs a decision: {cards[0].title}"
            )
            notification_id = _id("not")
            conn.execute(
                "INSERT INTO notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?)",
                (notification_id, decision_id, "decision_ready",
                 canonical_json({
                     "text": message,
                     "button": "Review decisions" if decision_type == "wiki_review" else "Open Decision Inbox",
                     "url": self.decision_url(decision_id),
                 }),
                 "PENDING", 0, iso(created), None, iso(created), None),
            )
            self._audit(conn, decision_id, "decision_published", source_profile, old_status, "READY",
                        {"cards_added": len(cards), "merged": merged})
        return self._publication_result(decision_id, source_surface, source_session_key, False, merged)

    def _publication_result(
        self, decision_id: str, source_surface: str | None, source_session_key: str | None,
        deduplicated: bool, merged: bool,
    ) -> dict[str, Any]:
        return {
            "decision_id": decision_id,
            "decision_url": self.decision_url(decision_id),
            "source_surface": source_surface,
            "delivery_mode": self.delivery_mode(source_surface, source_session_key),
            "deduplicated": deduplicated,
            "merged": merged,
        }

    def _insert_cards(self, conn: sqlite3.Connection, decision_id: str, cards, profile: str,
                      session: str, start: int, created: datetime, expires: datetime) -> None:
        for offset, card in enumerate(cards, 1):
            card_id = _id("card")
            execution = card.execution.model_dump(mode="json", exclude_none=True) if card.execution else None
            conn.execute(
                "INSERT INTO decision_cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, decision_id, start + offset, "wiki_patch" if execution else "choice", card.title,
                 card.summary, card.details, profile, session, card.recommendation.option_id,
                 canonical_json([e.model_dump(mode="json", exclude_none=True) for e in card.evidence]),
                 execution.get("execution_kind") if execution else None,
                 canonical_json(execution) if execution else None,
                 execution.get("base_sha256") if execution else None, 1, "READY", card.priority,
                 iso(created), iso(expires)),
            )
            options = [card.recommendation, *card.alternatives]
            for position, option in enumerate(options):
                option_execution = option.execution.model_dump(mode="json", exclude_none=True) if option.execution else None
                conn.execute(
                    "INSERT INTO decision_options("
                    "option_id,card_id,label,details,reason,is_recommended,position,execution_payload_json"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (option.option_id, card_id, option.label, option.details, option.reason,
                     1 if position == 0 else 0, position,
                     canonical_json(option_execution) if option_execution else None),
                )

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            decision = conn.execute("SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,)).fetchone()
            if not decision:
                raise NotFoundError("decision not found")
            cards = conn.execute(
                "SELECT * FROM decision_cards WHERE decision_id=? ORDER BY position", (decision_id,),
            ).fetchall()
            result_cards = []
            for card in cards:
                options = [dict(row) for row in conn.execute(
                    "SELECT option_id,label,details,reason,is_recommended,position,execution_payload_json "
                    "FROM decision_options WHERE card_id=? ORDER BY position", (card["card_id"],),
                ).fetchall()]
                response = conn.execute("SELECT * FROM card_responses WHERE card_id=?", (card["card_id"],)).fetchone()
                item = dict(card)
                item["evidence"] = json.loads(item.pop("evidence_json"))
                item["execution"] = json.loads(item.pop("execution_payload_json")) if item["execution_kind"] else None
                for option in options:
                    raw_execution = option.pop("execution_payload_json")
                    option["execution"] = json.loads(raw_execution) if raw_execution else None
                item["options"] = options
                item["response"] = dict(response) if response else None
                result_cards.append(item)
            output = dict(decision)
            output["cards"] = result_cards
            return output

    def publisher_inbox(
        self,
        profile: str,
        *,
        source_session_id: str | None = None,
        include_resolved: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return a minimal, profile-scoped read view for agent tooling."""
        fields = (
            "decision_id,name,title,source_profile,source_session_id,source_task_id,"
            "plugin_version,priority,status,decision_type,auto_resume,version,"
            "created_at,updated_at,expires_at"
        )
        with self.connect() as conn:
            clauses = ["source_profile=?"]
            params: list[Any] = [profile]
            if source_session_id:
                clauses.append("source_session_id=?")
                params.append(source_session_id)
            if not include_resolved:
                placeholders = ",".join("?" for _ in OPEN_STATES)
                clauses.append(f"status IN ({placeholders})")
                params.extend(sorted(OPEN_STATES))
            params.append(limit)
            rows = conn.execute(
                f"SELECT {fields} FROM decision_requests WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            items = []
            for row in rows:
                cards = conn.execute(
                    "SELECT card_id,title,summary,status,priority,position FROM decision_cards "
                    "WHERE decision_id=? ORDER BY position",
                    (row["decision_id"],),
                ).fetchall()
                items.append({**dict(row), "cards": [dict(card) for card in cards]})
            return items

    def inbox(self, tab: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if tab == "all":
                active_states = tuple(sorted(COMPLETED_STATES | HIDDEN_STATES))
                rows = conn.execute(
                    "SELECT * FROM decision_requests WHERE status NOT IN (%s) ORDER BY updated_at DESC"
                    % ",".join("?" for _ in active_states),
                    active_states,
                ).fetchall()
            elif tab == "completed":
                rows = conn.execute(
                    "SELECT * FROM decision_requests WHERE status IN (?,?,?) ORDER BY updated_at DESC",
                    tuple(sorted(COMPLETED_STATES)),
                ).fetchall()
            elif tab == "archive":
                archive_states = tuple(sorted(COMPLETED_STATES | HIDDEN_STATES))
                rows = conn.execute(
                    "SELECT * FROM decision_requests WHERE status IN (%s) ORDER BY updated_at DESC"
                    % ",".join("?" for _ in archive_states),
                    archive_states,
                ).fetchall()
            elif tab == "deferred":
                rows = conn.execute(
                    "SELECT DISTINCT d.* FROM decision_requests d JOIN decision_cards c ON c.decision_id=d.decision_id "
                    "JOIN card_responses r ON r.card_id=c.card_id WHERE r.outcome='deferred' "
                    "ORDER BY d.updated_at DESC",
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM decision_requests WHERE status NOT IN (?,?,?,?) AND decision_id NOT IN "
                    "(SELECT c.decision_id FROM decision_cards c JOIN card_responses r ON r.card_id=c.card_id "
                    "GROUP BY c.decision_id HAVING COUNT(*)=COUNT(CASE WHEN r.outcome='deferred' THEN 1 END)) "
                    "ORDER BY updated_at DESC", tuple(sorted(COMPLETED_STATES | HIDDEN_STATES)),
                ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                decision_id = row["decision_id"]
                first_card = conn.execute(
                    "SELECT summary FROM decision_cards WHERE decision_id=? ORDER BY position LIMIT 1",
                    (decision_id,),
                ).fetchone()
                latest = conn.execute(
                    "SELECT manifest_id,manifest_json FROM submission_manifests "
                    "WHERE decision_id=? ORDER BY submission_version DESC LIMIT 1",
                    (decision_id,),
                ).fetchone()
                manifest_id = latest["manifest_id"] if latest else None
                executable = False
                successful_apply = False
                if latest:
                    try:
                        manifest = json.loads(latest["manifest_json"])
                    except (TypeError, json.JSONDecodeError):
                        manifest = {}
                    executable = any(
                        response.get("outcome") in {"recommended", "alternative"}
                        and response.get("execution_kind") == "wiki_patch_v1"
                        for response in manifest.get("responses", [])
                    )
                    successful_apply = bool(conn.execute(
                        "SELECT 1 FROM execution_attempts WHERE manifest_id=? AND kind='wiki_apply' "
                        "AND status='COMPLETED' LIMIT 1", (manifest_id,),
                    ).fetchone())
                can_apply = (
                    row["decision_type"] == "wiki_review"
                    and executable
                    and not successful_apply
                    and row["status"] in {"READY_TO_APPLY", "APPLYING", "COMPLETED"}
                ) or (
                    row["decision_type"] == "wiki_review"
                    and row["status"] == "READY_TO_SUBMIT"
                )
                can_resume = (
                    row["decision_type"] == "ordinary"
                    and row["status"] == "READY_TO_SUBMIT"
                    and bool(row["auto_resume"])
                )
                can_submit = row["status"] == "READY_TO_SUBMIT"
                items.append({
                    **dict(row),
                    "summary": first_card["summary"] if first_card else "",
                    "card_count": conn.execute(
                        "SELECT COUNT(*) FROM decision_cards WHERE decision_id=?", (decision_id,),
                    ).fetchone()[0],
                    "can_apply": can_apply,
                    "can_resume": can_resume,
                    "can_submit": can_submit,
                    "can_discard": row["status"] in DISCARDABLE_STATES,
                    "can_archive": row["status"] in ARCHIVEABLE_STATES,
                    "can_edit": row["status"] in {"READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT"},
                    "apply_manifest_id": manifest_id if can_apply else None,
                    "apply_version": row["version"],
                })
            return items

    def respond(self, card_id: str, card_version: int, outcome: str, selected_option_id: str | None,
                note: str, telegram_user_id: int) -> dict[str, Any]:
        if outcome not in {"recommended", "alternative", "rejected", "deferred", "abstained"}:
            raise ValueError("invalid outcome")
        with self.transaction() as conn:
            card = conn.execute("SELECT * FROM decision_cards WHERE card_id=?", (card_id,)).fetchone()
            if not card:
                raise NotFoundError("card not found")
            if card["version"] != card_version:
                raise ConflictError("card version changed; reload before deciding")
            if card["status"] == "SUBMITTED":
                raise StateError("this card is part of an immutable submitted manifest")
            decision_status = conn.execute(
                "SELECT status FROM decision_requests WHERE decision_id=?", (card["decision_id"],),
            ).fetchone()["status"]
            if decision_status in {"EXPIRED", "CANCELLED"}:
                raise StateError(f"this decision is {decision_status.lower()}")
            if outcome == "recommended":
                selected_option_id = card["recommendation_option_id"]
            elif outcome == "alternative":
                if selected_option_id:
                    exists = conn.execute(
                        "SELECT 1 FROM decision_options WHERE card_id=? AND option_id=? AND is_recommended=0",
                        (card_id, selected_option_id),
                    ).fetchone()
                    if not exists:
                        raise ValueError("selected alternative is not valid for this card")
                elif not note.strip():
                    raise ValueError("custom alternative requires text")
            else:
                selected_option_id = None
            decided = iso()
            existing = conn.execute("SELECT response_id FROM card_responses WHERE card_id=?", (card_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE card_responses SET card_version=?,outcome=?,selected_option_id=?,note=?,telegram_user_id=?,decided_at=? "
                    "WHERE card_id=?",
                    (card_version, outcome, selected_option_id, note[:4000], telegram_user_id, decided, card_id),
                )
            else:
                conn.execute(
                    "INSERT INTO card_responses VALUES(?,?,?,?,?,?,?,?)",
                    (_id("resp"), card_id, card_version, outcome, selected_option_id, note[:4000], telegram_user_id, decided),
                )
            conn.execute("UPDATE decision_cards SET status=? WHERE card_id=?", ("DEFERRED" if outcome == "deferred" else "REVIEWING", card_id))
            decision_id = card["decision_id"]
            counts = conn.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN r.card_id IS NOT NULL THEN 1 ELSE 0 END) responded "
                "FROM decision_cards c LEFT JOIN card_responses r ON r.card_id=c.card_id WHERE c.decision_id=?",
                (decision_id,),
            ).fetchone()
            old = conn.execute("SELECT status FROM decision_requests WHERE decision_id=?", (decision_id,)).fetchone()["status"]
            new = "READY_TO_SUBMIT" if counts["total"] == counts["responded"] else "REVIEWING"
            conn.execute("UPDATE decision_requests SET status=?,updated_at=?,version=version+1 WHERE decision_id=?",
                         (new, decided, decision_id))
            self._audit(conn, decision_id, "card_response_saved", f"telegram:{telegram_user_id}", old, new,
                        {"card_id": card_id, "outcome": outcome})
        return self.get_decision(decision_id)

    def discard(self, decision_id: str, expected_version: int, telegram_user_id: int) -> dict[str, Any]:
        with self.transaction() as conn:
            decision = conn.execute(
                "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if not decision:
                raise NotFoundError("decision not found")
            if decision["version"] != expected_version:
                raise ConflictError("decision changed; reload before discarding")
            if decision["status"] not in DISCARDABLE_STATES:
                raise StateError("this decision can no longer be discarded")
            conn.execute(
                "UPDATE decision_cards SET status='CANCELLED' WHERE decision_id=? AND status!='SUBMITTED'",
                (decision_id,),
            )
            conn.execute(
                "UPDATE decision_requests SET status='CANCELLED',updated_at=?,version=version+1 WHERE decision_id=?",
                (iso(), decision_id),
            )
            self._audit(
                conn, decision_id, "decision_discarded", f"telegram:{telegram_user_id}",
                decision["status"], "CANCELLED",
            )
        return self.get_decision(decision_id)

    def archive(self, decision_id: str, expected_version: int, telegram_user_id: int) -> dict[str, Any]:
        with self.transaction() as conn:
            decision = conn.execute(
                "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if not decision:
                raise NotFoundError("decision not found")
            if decision["version"] != expected_version:
                raise ConflictError("decision changed; reload before archiving")
            if decision["status"] not in ARCHIVEABLE_STATES:
                raise StateError("this decision can no longer be archived")
            archived = iso()
            conn.execute(
                "UPDATE decision_requests SET status='ARCHIVED',updated_at=?,version=version+1 WHERE decision_id=?",
                (archived, decision_id),
            )
            self._audit(
                conn, decision_id, "decision_archived", f"telegram:{telegram_user_id}",
                decision["status"], "ARCHIVED",
            )
        return self.get_decision(decision_id)

    def submit(self, decision_id: str, expected_version: int, telegram_user_id: int) -> dict[str, Any]:
        created = now()
        with self.transaction() as conn:
            decision = conn.execute("SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,)).fetchone()
            if not decision:
                raise NotFoundError("decision not found")
            if decision["version"] != expected_version:
                raise ConflictError("decision changed; reload the summary before submitting")
            if decision["status"] != "READY_TO_SUBMIT":
                raise StateError("every card must be reviewed before submission")
            rows = conn.execute(
                "SELECT c.*,r.outcome,r.selected_option_id,r.note,r.decided_at FROM decision_cards c "
                "JOIN card_responses r ON r.card_id=c.card_id WHERE c.decision_id=? AND c.status!='SUBMITTED' "
                "ORDER BY c.position",
                (decision_id,),
            ).fetchall()
            if not rows:
                raise StateError("this decision has no unresolved cards to submit")
            responses = []
            for row in rows:
                execution = json.loads(row["execution_payload_json"]) if row["execution_kind"] else None
                execution_kind = row["execution_kind"]
                selected_option = None
                if row["selected_option_id"]:
                    selected_option = conn.execute(
                        "SELECT option_id,label,details,reason,execution_payload_json FROM decision_options "
                        "WHERE card_id=? AND option_id=?",
                        (row["card_id"], row["selected_option_id"]),
                    ).fetchone()
                if row["outcome"] == "alternative":
                    execution = (
                        json.loads(selected_option["execution_payload_json"])
                        if selected_option and selected_option["execution_payload_json"] else None
                    )
                    execution_kind = execution.get("execution_kind") if execution else None
                elif row["outcome"] not in {"recommended"}:
                    execution = None
                    execution_kind = None
                responses.append({
                    "card_id": row["card_id"], "card_version": row["version"], "title": row["title"],
                    "outcome": row["outcome"], "selected_option_id": row["selected_option_id"],
                    "selected_option_label": selected_option["label"] if selected_option else None,
                    "selected_option_details": selected_option["details"] if selected_option else None,
                    "selected_option_reason": selected_option["reason"] if selected_option else None,
                    "note": row["note"], "execution_kind": execution_kind,
                    "execution": execution,
                })
            previous = conn.execute(
                "SELECT COALESCE(MAX(submission_version),0) FROM submission_manifests WHERE decision_id=?",
                (decision_id,),
            ).fetchone()[0]
            submission_version = int(previous) + 1
            manifest = {
                "schema": "decision_manifest_v1", "status": "SUBMITTED", "decision_id": decision_id,
                "submission_version": submission_version, "source_profile": decision["source_profile"],
                "source_session_id": decision["source_session_id"],
                "source_task_id": decision["source_task_id"],
                "source_surface": decision["source_surface"],
                "telegram_user_id": telegram_user_id,
                "created_at": iso(created), "responses": responses,
            }
            encoded = canonical_json(manifest).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            manifest_id = _id("man")
            target_dir = self.manifest_root / decision_id
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"{digest}.json"
            path.write_bytes(encoded)
            os.chmod(path, 0o600)
            conn.execute(
                "INSERT INTO submission_manifests VALUES(?,?,?,?,?,?,?)",
                (manifest_id, decision_id, submission_version, digest, encoded.decode(), str(path), iso(created)),
            )
            active = [response for response in responses if response["outcome"] != "deferred"]
            executable = [response for response in active if response["execution_kind"] == "wiki_patch_v1"
                          and response["outcome"] in {"recommended", "alternative"}]
            if executable:
                new = "READY_TO_APPLY"
            elif decision["decision_type"] == "wiki_review" and active:
                new = "COMPLETED"
            elif active and bool(decision["auto_resume"]):
                new = "QUEUED_FOR_RESUME"
                kind = (
                    "conversation_resume"
                    if self.delivery_mode(decision["source_surface"], decision["source_session_key"]) == "conversation"
                    else "resume"
                )
                self._queue_execution(
                    conn, manifest_id, kind,
                    f"decision-{kind}:{decision_id}:{submission_version}", created,
                )
            elif active:
                new = "COMPLETED"
            else:
                new = "DEFERRED"
            for response in active:
                conn.execute("UPDATE decision_cards SET status='SUBMITTED' WHERE card_id=?", (response["card_id"],))
            old = decision["status"]
            conn.execute("UPDATE decision_requests SET status=?,updated_at=?,version=version+1 WHERE decision_id=?",
                         (new, iso(created), decision_id))
            summary = (
                f"Submitted: {sum(r['outcome'] == 'recommended' for r in responses)} accepted, "
                f"{sum(r['outcome'] == 'alternative' for r in responses)} alternatives, "
                f"{sum(r['outcome'] == 'rejected' for r in responses)} rejected, "
                f"{sum(r['outcome'] in {'deferred', 'abstained'} for r in responses)} abstained"
            )
            if new == "QUEUED_FOR_RESUME":
                summary += f"\nResuming {decision['source_profile'].title()}…"
            self._enqueue_notification(conn, decision_id, "submission", summary, None, created)
            self._audit(conn, decision_id, "submission_created", f"telegram:{telegram_user_id}", old, new,
                        {"manifest_id": manifest_id, "sha256": digest})
        return {"manifest_id": manifest_id, "sha256": digest, "status": new,
                "decision_version": expected_version + 1,
                "accepted": sum(r["outcome"] == "recommended" for r in responses),
                "alternatives": sum(r["outcome"] == "alternative" for r in responses),
                "rejected": sum(r["outcome"] == "rejected" for r in responses),
                "deferred": sum(r["outcome"] == "deferred" for r in responses),
                "abstained": sum(r["outcome"] == "abstained" for r in responses)}

    def _queue_execution(self, conn: sqlite3.Connection, manifest_id: str, kind: str,
                         key: str, created: datetime) -> str:
        existing = conn.execute("SELECT execution_id FROM execution_attempts WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return existing["execution_id"]
        execution_id = _id("exec")
        conn.execute(
            "INSERT INTO execution_attempts("
            "execution_id,manifest_id,kind,status,idempotency_key,run_id,attempt_count,error,result_json,"
            "created_at,updated_at,next_attempt_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (execution_id, manifest_id, kind, "QUEUED", key, None, 0, None, None,
             iso(created), iso(created), iso(created)),
        )
        return execution_id

    def queue_apply(self, manifest_id: str, expected_version: int) -> dict[str, Any]:
        with self.transaction() as conn:
            manifest = conn.execute(
                "SELECT m.*,d.status,d.decision_id FROM submission_manifests m JOIN decision_requests d "
                "ON d.decision_id=m.decision_id WHERE m.manifest_id=?", (manifest_id,),
            ).fetchone()
            if not manifest:
                raise NotFoundError("manifest not found")
            current_version = conn.execute(
                "SELECT version FROM decision_requests WHERE decision_id=?", (manifest["decision_id"],),
            ).fetchone()["version"]
            if current_version != expected_version:
                raise ConflictError("decision changed; reload before applying")
            if manifest["status"] not in {"READY_TO_APPLY", "APPLYING", "COMPLETED"}:
                raise StateError("manifest is not ready to apply")
            key = f"wiki-apply:{manifest['sha256']}"
            execution_id = self._queue_execution(conn, manifest_id, "wiki_apply", key, now())
            if manifest["status"] == "READY_TO_APPLY":
                conn.execute("UPDATE decision_requests SET status='APPLYING',updated_at=?,version=version+1 WHERE decision_id=?",
                             (iso(), manifest["decision_id"]))
                self._audit(conn, manifest["decision_id"], "wiki_apply_queued", "telegram", "READY_TO_APPLY", "APPLYING",
                            {"execution_id": execution_id})
        return self.get_execution(execution_id)

    def _enqueue_notification(self, conn: sqlite3.Connection, decision_id: str | None, kind: str,
                              text: str, button: str | None, created: datetime | None = None,
                              url: str | None = None) -> str:
        stamp = created or now()
        notification_id = _id("not")
        conn.execute(
            "INSERT INTO notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?)",
            (notification_id, decision_id, kind,
             canonical_json({"text": text, "button": button, "url": url}),
             "PENDING", 0, iso(stamp), None, iso(stamp), None),
        )
        return notification_id

    def enqueue_notification(self, decision_id: str | None, kind: str, text: str,
                             button: str | None = None, url: str | None = None) -> str:
        with self.transaction() as conn:
            return self._enqueue_notification(conn, decision_id, kind, text, button, url=url)

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM execution_attempts WHERE execution_id=?", (execution_id,)).fetchone()
            if not row:
                raise NotFoundError("execution not found")
            return dict(row)

    def claim_execution(self) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE status='QUEUED' "
                "AND kind IN ('resume','wiki_apply') ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE execution_attempts SET status='RUNNING',attempt_count=attempt_count+1,updated_at=? WHERE execution_id=?",
                         (iso(), row["execution_id"]))
            full = conn.execute(
                "SELECT e.*,m.manifest_json,m.manifest_path,m.sha256,d.source_profile,d.source_session_id,d.decision_id "
                "FROM execution_attempts e JOIN submission_manifests m ON m.manifest_id=e.manifest_id "
                "JOIN decision_requests d ON d.decision_id=m.decision_id WHERE e.execution_id=?",
                (row["execution_id"],),
            ).fetchone()
            if full["kind"] == "resume":
                old = conn.execute(
                    "SELECT status FROM decision_requests WHERE decision_id=?", (full["decision_id"],),
                ).fetchone()["status"]
                conn.execute(
                    "UPDATE decision_requests SET status='RESUMING',updated_at=?,version=version+1 WHERE decision_id=?",
                    (iso(), full["decision_id"]),
                )
                self._audit(conn, full["decision_id"], "resume_started", "service", old, "RESUMING",
                            {"execution_id": row["execution_id"]})
            return dict(full)

    def pending_continuations(self, profile: str, limit: int = 10) -> list[dict[str, Any]]:
        """List the oldest deliverable continuation for each session, preserving FIFO."""
        stamp = iso()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE execution_attempts SET status='QUEUED',lease_token=NULL,lease_expires_at=NULL,consumer_id=NULL "
                "WHERE kind='conversation_resume' AND status='CLAIMED' AND lease_expires_at<=?",
                (stamp,),
            )
            profile_clause = "" if profile == "*" else "AND d.source_profile=? "
            params: list[Any] = [] if profile == "*" else [profile]
            rows = conn.execute(
                "SELECT e.execution_id,e.status,e.lease_token,e.lease_expires_at,e.created_at,e.rowid AS queue_position,"
                "d.decision_id,d.source_profile,d.source_session_id,d.source_task_id,"
                "d.source_surface,d.source_session_key "
                "FROM execution_attempts e "
                "JOIN submission_manifests m ON m.manifest_id=e.manifest_id "
                "JOIN decision_requests d ON d.decision_id=m.decision_id "
                "WHERE e.kind='conversation_resume' " + profile_clause +
                "AND ((e.status='QUEUED' AND COALESCE(e.next_attempt_at,e.created_at)<=?) "
                "OR (e.status='DISPATCHED' AND e.lease_expires_at<=?)) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM execution_attempts p "
                "  JOIN submission_manifests pm ON pm.manifest_id=p.manifest_id "
                "  JOIN decision_requests pd ON pd.decision_id=pm.decision_id "
                "  WHERE p.kind='conversation_resume' "
                "  AND pd.source_profile=d.source_profile AND pd.source_session_id=d.source_session_id "
                "  AND p.rowid<e.rowid "
                "  AND p.status NOT IN ('COMPLETED','BLOCKED')"
                ") ORDER BY e.rowid LIMIT ?",
                (*params, stamp, stamp, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def _continuation_row(
        self, conn: sqlite3.Connection, execution_id: str, profile: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT e.*,e.rowid AS queue_position,m.manifest_json,m.sha256,d.decision_id,d.source_profile,d.source_session_id,"
            "d.source_task_id,d.source_surface,d.source_session_key "
            "FROM execution_attempts e "
            "JOIN submission_manifests m ON m.manifest_id=e.manifest_id "
            "JOIN decision_requests d ON d.decision_id=m.decision_id "
            "WHERE e.execution_id=? AND e.kind='conversation_resume'",
            (execution_id,),
        ).fetchone()
        if not row:
            raise NotFoundError("continuation not found")
        if profile != "*" and row["source_profile"] != profile:
            raise PermissionError("publisher profile mismatch")
        return row

    def claim_continuation(
        self, execution_id: str, profile: str, consumer_id: str, lease_seconds: int,
    ) -> dict[str, Any]:
        created = now()
        lease_seconds = max(15, min(300, int(lease_seconds)))
        with self.transaction() as conn:
            row = self._continuation_row(conn, execution_id, profile)
            if row["status"] != "QUEUED":
                raise ConflictError("continuation is not available to claim")
            if row["next_attempt_at"] and row["next_attempt_at"] > iso(created):
                raise ConflictError("continuation retry is not due")
            earlier = conn.execute(
                "SELECT 1 FROM execution_attempts p "
                "JOIN submission_manifests pm ON pm.manifest_id=p.manifest_id "
                "JOIN decision_requests pd ON pd.decision_id=pm.decision_id "
                "WHERE p.kind='conversation_resume' AND pd.source_profile=? AND pd.source_session_id=? "
                "AND p.rowid<? "
                "AND p.status NOT IN ('COMPLETED','BLOCKED') LIMIT 1",
                (row["source_profile"], row["source_session_id"], row["queue_position"]),
            ).fetchone()
            if earlier:
                raise ConflictError("an earlier continuation for this session is still pending")
            token = secrets.token_urlsafe(32)
            expires = created + timedelta(seconds=lease_seconds)
            conn.execute(
                "UPDATE execution_attempts SET status='CLAIMED',lease_token=?,lease_expires_at=?,consumer_id=?,"
                "attempt_count=attempt_count+1,error=NULL,updated_at=? WHERE execution_id=? AND status='QUEUED'",
                (token, iso(expires), consumer_id[:200], iso(created), execution_id),
            )
            self._audit(
                conn, row["decision_id"], "continuation_claimed", f"plugin:{consumer_id[:200]}",
                "QUEUED", "CLAIMED", {"execution_id": execution_id, "lease_expires_at": iso(expires)},
            )
            result = dict(row)
            result.update({"status": "CLAIMED", "lease_token": token, "lease_expires_at": iso(expires)})
            result["manifest"] = json.loads(result.pop("manifest_json"))
            return result

    def dispatch_continuation(self, execution_id: str, profile: str, lease_token: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._continuation_row(conn, execution_id, profile)
            if row["status"] != "CLAIMED" or not secrets.compare_digest(row["lease_token"] or "", lease_token):
                raise ConflictError("continuation lease is not valid for dispatch")
            conn.execute(
                "UPDATE execution_attempts SET status='DISPATCHED',updated_at=? WHERE execution_id=?",
                (iso(), execution_id),
            )
            old = conn.execute(
                "SELECT status FROM decision_requests WHERE decision_id=?", (row["decision_id"],),
            ).fetchone()["status"]
            conn.execute(
                "UPDATE decision_requests SET status='RESUMING',updated_at=?,version=version+1 WHERE decision_id=?",
                (iso(), row["decision_id"]),
            )
            self._audit(
                conn, row["decision_id"], "continuation_dispatched", "plugin", old, "RESUMING",
                {"execution_id": execution_id},
            )
        return self.get_execution(execution_id)

    def complete_continuation(
        self, execution_id: str, profile: str, lease_token: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._continuation_row(conn, execution_id, profile)
            if row["status"] == "COMPLETED":
                return dict(row)
            if row["status"] != "DISPATCHED" or not secrets.compare_digest(row["lease_token"] or "", lease_token):
                raise ConflictError("continuation lease is not valid for completion")
            conn.execute(
                "UPDATE execution_attempts SET status='COMPLETED',result_json=?,error=NULL,updated_at=? "
                "WHERE execution_id=?",
                (canonical_json(result), iso(), execution_id),
            )
            old = conn.execute(
                "SELECT status FROM decision_requests WHERE decision_id=?", (row["decision_id"],),
            ).fetchone()["status"]
            conn.execute(
                "UPDATE decision_requests SET status='ARCHIVED',updated_at=?,version=version+1 WHERE decision_id=?",
                (iso(), row["decision_id"]),
            )
            self._audit(
                conn, row["decision_id"], "continuation_completed", "plugin", old, "ARCHIVED",
                {"execution_id": execution_id},
            )
        return self.get_execution(execution_id)

    def fail_continuation(
        self, execution_id: str, profile: str, lease_token: str, error: str, retryable: bool,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._continuation_row(conn, execution_id, profile)
            if row["status"] not in {"CLAIMED", "DISPATCHED"} or not secrets.compare_digest(
                row["lease_token"] or "", lease_token,
            ):
                raise ConflictError("continuation lease is not valid for failure reporting")
            if retryable:
                delay = min(300, 2 ** min(int(row["attempt_count"]), 8))
                status = "QUEUED"
                next_attempt = iso(now() + timedelta(seconds=delay))
                decision_status = "QUEUED_FOR_RESUME"
            else:
                status = "BLOCKED"
                next_attempt = None
                decision_status = "BLOCKED"
            conn.execute(
                "UPDATE execution_attempts SET status=?,error=?,lease_token=NULL,lease_expires_at=NULL,"
                "consumer_id=NULL,next_attempt_at=?,updated_at=? WHERE execution_id=?",
                (status, error[:4000], next_attempt, iso(), execution_id),
            )
            old = conn.execute(
                "SELECT status FROM decision_requests WHERE decision_id=?", (row["decision_id"],),
            ).fetchone()["status"]
            conn.execute(
                "UPDATE decision_requests SET status=?,updated_at=?,version=version+1 WHERE decision_id=?",
                (decision_status, iso(), row["decision_id"]),
            )
            self._audit(
                conn, row["decision_id"], "continuation_retry" if retryable else "continuation_blocked",
                "plugin", old, decision_status, {"execution_id": execution_id, "error": error[:4000]},
            )
        return self.get_execution(execution_id)

    def wait_continuation(self, execution_id: str, profile: str, reason: str) -> dict[str, Any]:
        """Back off an inactive route and send its one out-of-band fallback alert."""
        with self.transaction() as conn:
            row = self._continuation_row(conn, execution_id, profile)
            if row["status"] != "QUEUED":
                return dict(row)
            existing = json.loads(row["result_json"] or "{}")
            alert_sent = bool(existing.get("waiting_alert_sent"))
            if not alert_sent:
                self._enqueue_notification(
                    conn, row["decision_id"], "continuation_waiting",
                    "Decision applied; waiting for the original task session to become active.",
                    "Open Decision Inbox", url=self.decision_url(row["decision_id"]),
                )
                self._audit(
                    conn, row["decision_id"], "continuation_waiting", "plugin", "QUEUED", "QUEUED",
                    {"execution_id": execution_id, "reason": reason[:1000]},
                )
            conn.execute(
                "UPDATE execution_attempts SET result_json=?,next_attempt_at=?,updated_at=? WHERE execution_id=?",
                (canonical_json({"waiting_alert_sent": True, "reason": reason[:1000]}),
                 iso(now() + timedelta(seconds=30)), iso(), execution_id),
            )
        return self.get_execution(execution_id)

    def set_execution_run_id(self, execution_id: str, run_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE execution_attempts SET run_id=?,updated_at=? WHERE execution_id=?",
                         (run_id, iso(), execution_id))

    def expire_due_decisions(self) -> int:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT decision_id,status FROM decision_requests WHERE expires_at<? AND status IN (%s)"
                % ",".join("?" for _ in EXPIRABLE_STATES),
                (iso(), *sorted(EXPIRABLE_STATES)),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE decision_requests SET status='EXPIRED',updated_at=?,version=version+1 WHERE decision_id=?",
                             (iso(), row["decision_id"]))
                self._audit(conn, row["decision_id"], "decision_expired", "service", row["status"], "EXPIRED")
            return len(rows)

    def finish_execution(self, execution_id: str, success: bool, result: dict[str, Any], error: str | None = None) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT e.*,m.decision_id FROM execution_attempts e JOIN submission_manifests m ON m.manifest_id=e.manifest_id "
                "WHERE e.execution_id=?", (execution_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("execution not found")
            decision_status = "ARCHIVED" if success else "BLOCKED"
            execution_status = "COMPLETED" if success else "BLOCKED"
            conn.execute(
                "UPDATE execution_attempts SET status=?,error=?,result_json=?,updated_at=? WHERE execution_id=?",
                (execution_status, (error or "")[:4000] or None, canonical_json(result), iso(), execution_id),
            )
            old = conn.execute("SELECT status FROM decision_requests WHERE decision_id=?", (row["decision_id"],)).fetchone()["status"]
            conn.execute(
                "UPDATE decision_requests SET status=?,updated_at=?,version=version+1 WHERE decision_id=?",
                (decision_status, iso(), row["decision_id"]),
            )
            self._audit(conn, row["decision_id"], "execution_finished", "service", old, decision_status,
                        {"execution_id": execution_id, "error": error})

    def archive_decision(self, decision_id: str, actor: str = "service") -> None:
        """Hide a successfully applied Wiki review while retaining its audit trail."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM decision_requests WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("decision not found")
            if row["status"] == "ARCHIVED":
                return
            old = row["status"]
            conn.execute(
                "UPDATE decision_requests SET status='ARCHIVED',updated_at=?,version=version+1 WHERE decision_id=?",
                (iso(), decision_id),
            )
            self._audit(conn, decision_id, "decision_archived", actor, old, "ARCHIVED")

    def claim_notification(self) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE status IN ('PENDING','RETRY') AND next_attempt_at<=? "
                "ORDER BY created_at LIMIT 1", (iso(),),
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE notification_outbox SET status='SENDING',attempt_count=attempt_count+1 WHERE notification_id=?",
                         (row["notification_id"],))
            return dict(row)

    def finish_notification(self, notification_id: str, success: bool, error: str | None = None) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM notification_outbox WHERE notification_id=?", (notification_id,)).fetchone()
            if not row:
                raise NotFoundError("notification not found")
            if success:
                conn.execute("UPDATE notification_outbox SET status='SENT',sent_at=?,last_error=NULL WHERE notification_id=?",
                             (iso(), notification_id))
                if row["decision_id"]:
                    decision = conn.execute("SELECT status FROM decision_requests WHERE decision_id=?", (row["decision_id"],)).fetchone()
                    if decision and decision["status"] == "READY":
                        conn.execute("UPDATE decision_requests SET status='NOTIFIED',updated_at=?,version=version+1 WHERE decision_id=?",
                                     (iso(), row["decision_id"]))
                        self._audit(conn, row["decision_id"], "notification_sent", "service", "READY", "NOTIFIED")
            else:
                delay = min(3600, 2 ** min(row["attempt_count"], 10))
                retry_at = now() + timedelta(seconds=delay)
                conn.execute("UPDATE notification_outbox SET status='RETRY',next_attempt_at=?,last_error=? WHERE notification_id=?",
                             (iso(retry_at), (error or "unknown error")[:2000], notification_id))
