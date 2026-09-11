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

from hermes_decision_inbox.schemas import DecisionRequest


OPEN_STATES = {
    "DRAFT", "READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT",
    "SUBMITTED", "QUEUED_FOR_RESUME", "RESUMING", "READY_TO_APPLY", "APPLYING", "BLOCKED",
}
COMPLETED_STATES = {"COMPLETED", "EXPIRED", "CANCELLED"}
HIDDEN_STATES = {"ARCHIVED"}
EXPIRABLE_STATES = {"DRAFT", "READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT"}
MERGEABLE_STATES = {"READY", "NOTIFIED", "REVIEWING", "DEFERRED", "READY_TO_SUBMIT"}


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
                 auto_resume: bool = True, weekly_only: bool = False):
        self.path = Path(path)
        self.manifest_root = Path(manifest_root)
        self.default_expiry_days = default_expiry_days
        self.auto_resume = auto_resume
        self.weekly_only = weekly_only

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
            conn.execute("UPDATE notification_outbox SET status='RETRY' WHERE status='SENDING'")
            conn.execute("UPDATE execution_attempts SET status='QUEUED' WHERE status='RUNNING'")
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

    def publish(self, raw: dict[str, Any], credential_profile: str) -> dict[str, Any]:
        source_profile = str(raw.get("source_profile") or "").strip()
        if credential_profile != "*" and source_profile != credential_profile:
            raise PermissionError("publisher credential does not match source_profile")
        source_session = str(raw.get("source_session_id") or "").strip()
        source_task = str(raw.get("source_task_id") or source_session).strip()
        if not source_profile or not source_session:
            raise ValueError("source_profile and source_session_id are required")
        parsed = DecisionRequest.model_validate({
            key: value for key, value in raw.items()
            if key not in {
                "source_profile", "source_session_id", "source_task_id", "plugin_version",
                "auto_resume", "default_expiry_days",
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
                return {"decision_id": duplicate["decision_id"], "deduplicated": True, "merged": False}

            existing = conn.execute(
                "SELECT decision_id,status,decision_type FROM decision_requests WHERE source_profile=? AND source_session_id=? "
                "AND status IN (%s) ORDER BY created_at DESC LIMIT 1" % ",".join("?" for _ in OPEN_STATES),
                (source_profile, source_session, *sorted(OPEN_STATES)),
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
                    "UPDATE decision_requests SET updated_at=?,version=version+1,status='READY' WHERE decision_id=?",
                    (iso(created), decision_id),
                )
                old_status = existing["status"]
                merged = True
            else:
                start = 0
                conn.execute(
                    "INSERT INTO decision_requests("
                    "decision_id,name,title,source_profile,source_session_id,source_task_id,plugin_version,priority,"
                    "status,decision_type,dedupe_hash,auto_resume,version,created_at,updated_at,expires_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, parsed.name, title, source_profile, source_session, source_task,
                     str(raw.get("plugin_version") or "unknown"), parsed.priority, "READY", decision_type,
                     payload_hash, 1 if request_auto_resume else 0, 1, iso(created), iso(created), iso(expires)),
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
                 canonical_json({"text": message, "button": "Review decisions" if decision_type == "wiki_review" else "Open Decision Inbox"}),
                 "PENDING", 0, iso(created), None, iso(created), None),
            )
            self._audit(conn, decision_id, "decision_published", source_profile, old_status, "READY",
                        {"cards_added": len(cards), "merged": merged})
        return {"decision_id": decision_id, "deduplicated": False, "merged": merged}

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

    def inbox(self, tab: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if tab == "all":
                rows = conn.execute(
                    "SELECT * FROM decision_requests ORDER BY updated_at DESC",
                ).fetchall()
            elif tab == "completed":
                rows = conn.execute(
                    "SELECT * FROM decision_requests WHERE status IN (?,?,?) ORDER BY updated_at DESC",
                    tuple(sorted(COMPLETED_STATES)),
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
                items.append({
                    **dict(row),
                    "card_count": conn.execute(
                        "SELECT COUNT(*) FROM decision_cards WHERE decision_id=?", (decision_id,),
                    ).fetchone()[0],
                    "can_apply": can_apply,
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
                if row["outcome"] == "alternative":
                    option = conn.execute(
                        "SELECT execution_payload_json FROM decision_options WHERE card_id=? AND option_id=?",
                        (row["card_id"], row["selected_option_id"]),
                    ).fetchone()
                    execution = json.loads(option["execution_payload_json"]) if option and option["execution_payload_json"] else None
                    execution_kind = execution.get("execution_kind") if execution else None
                elif row["outcome"] not in {"recommended"}:
                    execution = None
                    execution_kind = None
                responses.append({
                    "card_id": row["card_id"], "card_version": row["version"], "title": row["title"],
                    "outcome": row["outcome"], "selected_option_id": row["selected_option_id"],
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
                "source_session_id": decision["source_session_id"], "telegram_user_id": telegram_user_id,
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
                self._queue_execution(conn, manifest_id, "resume", f"decision-resume:{decision_id}:{submission_version}", created)
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
        conn.execute("INSERT INTO execution_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (execution_id, manifest_id, kind, "QUEUED", key, None, 0, None, None, iso(created), iso(created)))
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
                              text: str, button: str | None, created: datetime | None = None) -> str:
        stamp = created or now()
        notification_id = _id("not")
        conn.execute(
            "INSERT INTO notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?)",
            (notification_id, decision_id, kind, canonical_json({"text": text, "button": button}),
             "PENDING", 0, iso(stamp), None, iso(stamp), None),
        )
        return notification_id

    def enqueue_notification(self, decision_id: str | None, kind: str, text: str,
                             button: str | None = None) -> str:
        with self.transaction() as conn:
            return self._enqueue_notification(conn, decision_id, kind, text, button)

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM execution_attempts WHERE execution_id=?", (execution_id,)).fetchone()
            if not row:
                raise NotFoundError("execution not found")
            return dict(row)

    def claim_execution(self) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM execution_attempts WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
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
            new_status = "COMPLETED" if success else "BLOCKED"
            conn.execute(
                "UPDATE execution_attempts SET status=?,error=?,result_json=?,updated_at=? WHERE execution_id=?",
                (new_status, (error or "")[:4000] or None, canonical_json(result), iso(), execution_id),
            )
            old = conn.execute("SELECT status FROM decision_requests WHERE decision_id=?", (row["decision_id"],)).fetchone()["status"]
            conn.execute(
                "UPDATE decision_requests SET status=?,updated_at=?,version=version+1 WHERE decision_id=?",
                (new_status, iso(), row["decision_id"]),
            )
            self._audit(conn, row["decision_id"], "execution_finished", "service", old, new_status,
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
