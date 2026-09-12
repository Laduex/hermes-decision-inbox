from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_routing_skill_bounds_session_search_and_defines_fallback():
    text = (ROOT / "skills/decision-inbox-routing/SKILL.md").read_text(encoding="utf-8")

    assert "Never emit parallel or batched" in text
    assert "at most six `session_search` calls" in text
    assert "Never call" in text and "argument object already present" in text
    assert "idempotent_no_progress_warning" in text
    assert "identical_call_streak_halt" in text
    assert "hermes sessions export - --format jsonl --only user-prompts" in text
    assert "top-level arguments `name` and `batch`" in text


def test_deployment_contract_carries_session_search_guardrails():
    text = (ROOT / "docs/weekly-memory-wiki-review.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "never in a parallel tool-call batch" in normalized
    assert "at most six total `session_search` calls" in normalized
    assert "never reuse an argument object" in normalized
    assert "redacted, date-bounded" in normalized
    assert "Coverage and uncertainty" in normalized


def test_apply_resume_smoke_skill_defines_one_card_and_resumed_reply():
    text = (ROOT / "skills/decision-inbox-apply-resume-smoke/SKILL.md").read_text(encoding="utf-8")

    assert "Call `publish_decision` exactly once" in text
    assert "option_id`: `received`" in text
    assert "After the tool returns `PUBLISHED`, return exactly `[SILENT]`" in text
    assert "Decision Inbox smoke test passed: your decision was received and applied to this session." in text
    assert "Do not use from a delegated session" in text
