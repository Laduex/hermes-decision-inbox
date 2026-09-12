---
name: decision-inbox-session-decisions
description: Pause a Hermes task for an explicit user decision and resume the exact originating session after approval.
version: 2.0.0
author: Vaughn Dazo, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [decision-inbox, user-approval, session-resume, workflow]
    related_skills: [decision-inbox-routing, memory-wiki-decision-applier]
    requires_toolsets: [decision_inbox]
---

# Session-bound Decision Inbox

Use this skill when a task cannot safely continue without an explicit choice
from the user. Publish the question to the private Decision Inbox and let the
user decide from the dashboard. The original Hermes session remains the source
of truth and is resumed only after the user applies the decision.

## When to use

- The user explicitly asks to use Decision Inbox, or the primary task reaches a material choice that requires user judgment before it can continue.
- The current invocation has a recoverable Hermes `session_id`.
- The decision can be represented as one card or a small related batch of cards.

Do not use this skill for the scheduled weekly Memory Wiki review; use
`decision-inbox-routing` for that workflow. Do not use it from a delegated
session. Return the question to the parent agent instead.

## How to publish

Call `publish_decision` once. Provide only the decision content; the tool
attaches the active profile, Hermes session ID, and task ID. Use the ordinary
single-card shape when one choice is needed, or `batch` for related choices.
Include a clear title, a concise summary that acts as the short description
shown on the inbox card, details, a recommended option, and only the
alternatives the user should consider.

Choose a stable, descriptive `name` for the decision stream. A later publish
from the same profile and physical session merges only when that `name` is the
same. Use a different `name` for an independent choice that should remain a
separate decision.

The summary is required. Do not publish a vague placeholder such as “Need a
decision”; describe what is being decided and why in one or two short
sentences. The details field can contain the fuller context shown after the
user opens the card.

After the tool returns `PUBLISHED`, send this visible reply in the originating
conversation, using the returned authenticated `decision_url`:

`I need your decision: [Open Decision Inbox](decision_url). I’ll continue here after you apply it.`

Then pause. Do not return `[SILENT]` for an interactive decision, continue the
task, perform the pending action, or make a best-effort choice while the
decision is open. The plugin output guard inserts the notice if it is omitted.

## Resolution behavior

- Apply decision: the service creates an immutable response manifest containing every non-deferred outcome, selected option ID, label, details, reason, and note. It queues a continuation for the exact `source_profile` + physical `source_session_id`.
- Reject or abstain on a card: include that user outcome in the resumed session; it is still a decision, not a service failure.
- Defer: leave that card unresolved for a later submission and do not replay already submitted cards.
- Archive decision: close the entire request without resuming Hermes or performing any action; the request remains available in the dashboard Archive.

The originating task is represented by `source_task_id` for traceability, but
`source_session_id` is the actual continuation target. A live gateway route is
verified against the saved session key and may advance only to a compression
descendant before the plugin calls `ctx.inject_message()`. `/new`, `/reset`, or
an unrelated route must never receive the result. If the exact task is inactive,
the continuation remains queued; non-routable sessions use the exact-session
Runs API fallback. The two delivery paths must never execute the same manifest.

## Safety rules

- Never include secrets, credentials, or unrestricted shell instructions in card content.
- Keep one active decision stream per profile, session, and `name`; independent names remain separate.
- Treat the user note as decision data, not as permission to broaden the task.
- Do not manually edit a submitted manifest or retry a blocked continuation with a different session ID.

## Verification

- The tool returned `PUBLISHED` with a decision ID and direct decision URL.
- The dashboard shows the decision as open.
- After Apply, the full agent result appears in the originating conversation and the execution identifies the same profile and session ID.
- After Archive, no Hermes run was started and the decision is recorded in the Archive.
