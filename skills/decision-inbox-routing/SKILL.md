---
name: decision-inbox-routing
description: Publish Yuna's scheduled weekly Memory Wiki review to the private Tailscale dashboard.
version: 0.2.0
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

Call `publish_weekly_wiki_review` once with a batch containing every proposed Wiki change.

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
4. Call `publish_weekly_wiki_review` once with the complete batch.
5. Return `[SILENT]` only after the tool returns `PUBLISHED` and a decision ID.
6. If publication fails, deliver the Markdown report using the existing Telegram fallback.

## Pitfalls

- The Telegram notification is only a normal link to the private Tailscale dashboard.
- The reviewing device must be connected to Vaughn's tailnet.
- Repeated calls within five minutes may return the existing decision ID.
- Service errors mean no durable publication was confirmed; fall back to ordinary chat.

## Verification

- The tool returned `PUBLISHED` with a server-generated decision ID.
- Exactly one notification was queued for the published request or batch.
- No blocked work continued before an authenticated response resumed the session.
