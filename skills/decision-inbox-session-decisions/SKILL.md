---
name: decision-inbox-session-decisions
description: Pause a Hermes task for an explicit user decision and resume the exact originating session after approval.
version: 1.5.0
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

- The task needs a user choice, approval, rejection, or clarification before it can continue.
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

The summary is required. Do not publish a vague placeholder such as “Need a
decision”; describe what is being decided and why in one or two short
sentences. The details field can contain the fuller context shown after the
user opens the card.

After the tool returns `PUBLISHED`, stop the current response with `[SILENT]`.
Do not continue the task, perform the pending action, or make a best-effort
choice while the decision is open.

## Resolution behavior

- Apply decision: the service creates an immutable response manifest and queues a resume of the exact `source_profile` + `source_session_id`.
- Reject or abstain on a card: include that user outcome in the resumed session; it is still a decision, not a service failure.
- Defer: leave that card unresolved for a later submission and do not replay already submitted cards.
- Archive decision: close the entire request without resuming Hermes or performing any action; the request remains available in the dashboard Archive.

The originating cron job is represented by `source_task_id` for traceability,
but `source_session_id` is the actual continuation target. If the session is no
longer recoverable, the worker must report a blocked execution rather than
starting a new unrelated session.

## Safety rules

- Never include secrets, credentials, or unrestricted shell instructions in card content.
- Keep one active decision stream per profile and session; related new cards may be merged into it.
- Treat the user note as decision data, not as permission to broaden the task.
- Do not manually edit a submitted manifest or retry a blocked continuation with a different session ID.

## Verification

- The tool returned `PUBLISHED` with a decision ID.
- The dashboard shows the decision as open.
- After Apply, the execution result identifies the same profile and session ID.
- After Archive, no Hermes run was started and the decision is recorded in the Archive.
