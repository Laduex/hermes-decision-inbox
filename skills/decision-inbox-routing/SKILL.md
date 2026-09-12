---
name: decision-inbox-routing
description: Publish Yuna's scheduled weekly Memory Wiki review to the private Tailscale dashboard.
version: 0.2.1
author: Vaughn Dazo, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [tailscale, wiki, weekly-review, dashboard]
    related_skills: [memory-wiki-decision-applier]
    requires_toolsets: [decision_inbox]
---

# Weekly Memory Wiki Dashboard Publisher

Publish one structured card batch after Yuna completes the Sunday read-only Memory Wiki review. This skill is not available for ordinary Hermes decisions.

## When to Use

- Use only for the scheduled `Weekly Memory Wiki Review` owned by Yuna/default.
- Do not use from conversations, other profiles, delegated agents, or unrelated cron jobs.

## Prerequisites

- The `decision_inbox` toolset is enabled for the active primary profile.
- The plugin has a service URL in `plugins.entries.hermes-decision-inbox.settings.service_url`.
- `DECISION_INBOX_PUBLISH_TOKEN` is available in the profile's secret scope.

## How to Run

Call `publish_weekly_wiki_review` once with top-level arguments `name` and `batch`
only. Put title, summary, details, evidence, priority, recommendation, alternatives,
and execution inside each batch card. The batch contains every proposed Wiki change.

## Session History Retrieval

Keep session discovery bounded so a thin or unchanged result cannot prevent report publication:

1. Issue `session_search` calls sequentially. Never emit parallel or batched
   `session_search` calls in one assistant turn.
2. Make at most six `session_search` calls in the entire review, including browse,
   discovery, read, and scroll calls. A no-argument browse may be called at most once.
3. Keep a short ledger of argument objects already used. Never call
   `session_search` again with an argument object already present in the ledger.
4. Search with review-specific topic terms. If a result is empty, broaden that topic
   once with materially different FTS5 terms such as `alpha OR beta`; then move on.
5. If a result reports `idempotent_no_progress_warning`, `identical_call_streak_halt`,
   or otherwise provides no new sessions, stop using `session_search` for this run.
   Switch to a redacted, date-bounded session inventory using
   `hermes sessions export - --format jsonl --only user-prompts --after <start> --before <end-exclusive> --redact`,
   then continue with the available Wiki, Hindsight, candidate-file, and session evidence.
6. Retrieval gaps lower confidence and belong in **Coverage and uncertainty**. They do
   not justify retrying unchanged calls or abandoning a safe proposal-only report.

## Quick Reference

| Choice | Meaning |
| --- | --- |
| Swipe right / Right arrow | Accept the recommended change |
| Swipe left / Left arrow | Reject the change |
| Swipe up / Up arrow | Abstain and archive without a change |
| Swipe down / Down arrow | Open alternative answers |

## Procedure

1. Complete the existing seven-day read-only review and Markdown report.
2. Convert every proposed edit into a card with an exact target, operation, base hash, section anchor when needed, and proposed Markdown.
3. Put alternate wording in option-level `execution` payloads so selecting it applies that exact text.
4. Call `publish_weekly_wiki_review` once with the complete batch. Do not include
   single-card fields such as `title`, `summary`, `details`, or `recommendation` at
   the top level.
5. Return `[SILENT]` only after the tool returns `PUBLISHED` and a decision ID.
6. If publication fails, deliver the Markdown report using the existing Telegram fallback.

## Pitfalls

- The Telegram notification is only a normal link to the private Tailscale dashboard.
- The reviewing device must be connected to Vaughn's tailnet.
- Repeated calls within five minutes may return the existing decision ID.
- Reusing `session_search` arguments can trip the unattended-run loop guard before
  staging. Follow the sequential call budget and export fallback above.
- Service errors mean no durable publication was confirmed; fall back to ordinary chat.

## Verification

- The tool returned `PUBLISHED` with a server-generated decision ID.
- Exactly one notification was queued for the published request or batch.
- No blocked work continued before an authenticated response resumed the session.
