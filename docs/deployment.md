# Production deployment handoff

## Production locations

```text
Code:       /srv/laduex/apps/hermes-decision-inbox
State:      /srv/laduex/state/hermes-decision-inbox
Secrets:    /etc/laduex/env/hermes-decision-inbox.env
Backups:    /srv/laduex/backups/hermes-decision-inbox
Review data:/opt/data/decision-inbox/reviews
Wiki:       /opt/data/home/wiki
```

The service binds to `127.0.0.1:8088`. Tailscale Serve publishes private HTTPS on port `8451` at `https://hermes-prod.tail4766ea.ts.net:8451`. The backend authorizes only the configured `Tailscale-User-Login`. Telegram sends one batch-ready link but does not authenticate the dashboard and is never consumed by this service.

## Preflight and backups

1. Record the source revision, wheel SHA-256, image digest, schema version, and operator time under `/srv/laduex/ops`.
2. Create mode-600 backups of Hermes configuration, the Yuna profile configuration, weekly cron definition, relevant skills, SQLite state, Tailscale Serve configuration, and complete Wiki.
3. Back up Wiki ACLs before granting the service UID only the access required by the executor.
4. Verify no production secret is embedded in YAML, the wheel, image, logs, or browser assets.
5. Keep the public Cloudflare route available only until the Tailscale endpoint and owner authentication are verified; then disable the public route.

## Hermes boundary

Install the pinned wheel into the derived Hermes image without editing `/opt/hermes` source. Enable `hermes-decision-inbox` only for Yuna/default and expose `decision_inbox` only to the cron surface. Other profiles and delegated agents cannot publish cards.

The weekly cron calls `publish_weekly_wiki_review` with one batch. Every card must include an exact relative Wiki path, current base SHA-256, supported operation, and proposed content. The tool returns `PUBLISHED`; the cron then returns `[SILENT]`. Publication failure uses the existing Markdown report delivery.

## Canary sequence

1. Start the service with Wiki execution disabled and verify health, Tailscale owner login, keyboard controls, and mobile gestures.
2. Publish one synthetic executable card against a disposable Wiki mount and verify immutable manifest creation.
3. Enable the deterministic executor and apply the disposable card; verify backup, audit entry, changed-file boundary, and receipt.
4. Update cron job `3bdf2dbbc400`, run a read-only manual review, and verify one batch plus one Telegram link.
5. Leave canonical Wiki mutation dependent on Vaughn pressing `Apply selected Wiki changes`.

Restart only affected Compose services. Never use `docker compose down`. If Hermes must reload the plugin or cron, restart only the `hermes` service and verify container health, gateway heartbeat, Telegram reconnection, and all expected profile adapters.

## Rollback

1. Restore the previous weekly cron definition.
2. Remove `decision_inbox` from the Yuna cron toolset and disable the plugin.
3. Restart only Hermes and verify the original weekly Markdown report path.
4. Stop the dashboard service while retaining SQLite, manifests, audit events, and receipts.
5. Restore the prior Tailscale Serve configuration.
6. Restore the Wiki backup only if a verified apply failure changed content incorrectly.

Plugin, cron, dashboard, route, and Wiki rollback remain independently reversible.
