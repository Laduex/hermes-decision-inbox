---
name: decision-inbox-apply-resume-smoke
description: Verify one applied decision resumes the agent session.
version: 1.5.0
author: Vaughn Dazo, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    category: testing
    tags: [decision-inbox, smoke-test, session-resume, workflow]
    related_skills: [decision-inbox-session-decisions]
    requires_toolsets: [decision_inbox]
---

# Decision Inbox Apply and Resume Smoke Test

Run one harmless end-to-end check of Decision Inbox publication, user Apply, and exact-session resumption. This Skill is for local validation or an explicitly requested canary; it does not apply Wiki changes or other external actions.

## When to Use

- Use when verifying that a user can receive one Decision Inbox card and Apply it.
- Use only from a recoverable primary Hermes session, including a deliberately created cron session.
- Do not use from a delegated session or as part of the weekly Memory Wiki review.

## Prerequisites

- The `decision_inbox` toolset is enabled.
- The Decision Inbox plugin and service are configured.
- The current Hermes session can be resumed after the user responds.

## How to Run

1. Call `publish_decision` exactly once with one card and no execution payload.
2. Ask: **Did you receive this Decision Inbox smoke test?**
3. Use this recommendation:
   - `option_id`: `received`
   - `label`: `Yes, I received it`
   - `reason`: `Confirms that the Decision Inbox can return the user's choice to the originating session.`
4. After the tool returns `PUBLISHED`, return exactly `[SILENT]`. Do not continue the task, poll the service, or publish another card.

## Resumed Session

When the original session receives the authenticated Decision Inbox response after Apply:

- Confirm that the selected option is `received` and the outcome is `recommended`.
- Reply exactly: `Decision Inbox smoke test passed: your decision was received and applied to this session.`
- If the outcome is rejected, deferred, or another option, report that outcome as a failed smoke test and do not claim success.

## Verification

- The first turn returned `[SILENT]` after one `PUBLISHED` response.
- The dashboard showed one pending decision tied to the originating profile and session.
- Applying the recommendation resumed the same profile and session.
- The resumed agent returned the smoke-test pass message.
