# Weekly Memory Wiki Review integration

Keep job `3bdf2dbbc400` scheduled for Sunday at 21:00 Asia/Manila and owned by Yuna/default. Replace its task instructions only during the production rollout, after the Decision Inbox canary passes.

## Replacement task instructions

1. Review the previous seven days in read-only mode.
2. Retrieve session history sequentially, never in a parallel tool-call batch. Make at
   most six total `session_search` calls, call no-argument browse at most once, and
   never reuse an argument object. After an empty result, try at most one materially
   broader FTS5 query for that topic.
3. On `idempotent_no_progress_warning`, `identical_call_streak_halt`, or another
   unchanged result, stop session search and switch to a redacted, date-bounded
   `hermes sessions export` inventory. Continue the review from the available evidence
   and disclose the retrieval gap under **Coverage and uncertainty**.
4. Write the existing human-readable Markdown report.
5. Create one `publish_weekly_wiki_review` batch containing every proposed Wiki change.
6. For each executable card, include `execution_kind: wiki_patch_v1`, an exact path relative to `/opt/data/home/wiki`, one supported operation, the current target SHA-256, section anchor when required, and exact proposed Markdown.
7. Publish one batch. The review must not modify the Wiki, Hindsight, profiles, skills, repositories, or any external system.
8. Return `[SILENT]` only after `publish_weekly_wiki_review` confirms `PUBLISHED`.
9. If publication fails, deliver the Markdown report through the existing Telegram fallback and report the publishing blocker truthfully.

Store staging artifacts beneath `/opt/data/decision-inbox/reviews/<review-id>/`, outside the canonical Wiki.
