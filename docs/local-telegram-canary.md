# Local private-dashboard canary

Use an isolated Hermes home and disposable Wiki. Never provide a production bot token, Hermes key, Wiki mount, or profile state.

## Prepare

```text
cd /Users/vaughndazo/Documents/LDX/Apps/hermes-decision-inbox
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
./scripts/setup_local_home.sh
cp .env.example .env
```

Generate independent signing and publisher secrets. Set `DECISION_INBOX_TAILSCALE_OWNER_LOGIN` to the test tailnet identity. Keep Wiki execution disabled for the interface pass.

## Start

```text
docker compose up --build decision-inbox
```

Or run directly:

```text
.venv/bin/uvicorn service.app.main:app --host 127.0.0.1 --port 8080
cd web
npm ci
npm run dev -- --host 127.0.0.1
```

Publish the local origin with Tailscale Serve, for example `tailscale serve --bg --https=8451 http://127.0.0.1:8080`. Do not expose the service through a public tunnel.

## Verify the interface

1. Publish a harmless synthetic weekly Wiki card with `decision-inbox publish-test`.
2. Confirm exactly one Telegram notification is sent when notification credentials are configured.
3. Open the private URL and verify Review and Completed tabs.
4. Verify Right/Left/Up/Down mean Accept/Reject/Abstain/Alternatives.
5. Repeat with mobile swipes and visible buttons.
6. Close and reopen the browser; saved responses must remain.
7. Submit the review and verify no write occurs until the Apply confirmation.

## Verify the Wiki executor

Copy a synthetic Markdown fixture into `.local/fixtures/wiki`. Enable the Wiki executor, publish a `wiki_patch_v1` card with its current SHA-256, accept it, and press Apply. Confirm a mode-600 backup, one approved changed file, audit log, and matching receipt. Alter the target and verify the stale batch blocks before mutation.
