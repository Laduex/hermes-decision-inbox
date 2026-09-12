# Hermes Decision Inbox

`hermes-decision-inbox` is a standalone Hermes plugin and private web dashboard for explicit user decisions. The dashboard runs continuously behind Tailscale. Any primary Hermes task may publish a decision when the user requests it or a material choice needs user judgment; the tool returns a direct link in that conversation and Apply continues the exact originating task. The Sunday Weekly Memory Wiki Review remains a separate publisher contract.

The cron never edits the canonical Wiki. Telegram is an out-of-band fallback and sends the same decision-specific link to the private dashboard. The service does not consume Telegram updates and does not change Hermes core. Routable sessions use the trusted plugin's gateway injection path; non-routable and legacy sessions retain the exact-session Runs API fallback. A user can apply a session decision, apply selected Wiki changes, or archive the entire decision without resuming the task.

## Interaction model

- Right arrow or swipe right: accept the recommendation.
- Left arrow or swipe left: reject the proposal.
- Up arrow or swipe up: abstain and archive without a Wiki change.
- Down arrow or swipe down: open exact alternative answers.
- `Apply selected Wiki changes`: freeze an immutable manifest and apply only accepted recommendations or selected alternatives.

## Repository layout

- `src/hermes_decision_inbox/`: Hermes plugin, weekly, general, and read-only decision tools, and diagnostics CLI.
- `service/app/`: FastAPI service, SQLite repository, notification outbox, and deterministic Wiki worker.
- `web/`: keyboard- and touch-friendly web dashboard built with TypeScript and Vite.
- `skills/`: session-decision, read-decision, weekly-review, and Wiki-apply instructions registered by the plugin.
- `tests/`: isolated schema, state, identity, plugin, UI-gesture, and Wiki executor tests.

The matching Hermes source worktree is `/Users/vaughndazo/Documents/LDX/Apps/hermes-agent` on branch `feat/telegram-decision-inbox`. It remains unchanged because this feature uses the public plugin interface.

## Local development

```text
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
./scripts/setup_local_home.sh
npm --prefix web ci
./scripts/start_local_dashboard.sh
```

`start_local_dashboard.sh` runs an isolated FastAPI service on `127.0.0.1:8080`
and the Vite dashboard on `127.0.0.1:5173`. Vite proxies `/api` (including the
WebSocket) to the local service and injects a development-only owner identity,
so browser authentication works without Tailscale credentials. State and the
disposable Wiki fixture stay under `.local/`; no production database, bot token,
Wiki, or Hermes API key is used. Keep the script running while editing files in
`web/src` to get HMR; stop it with Ctrl-C before starting another local session.

To populate a harmless local card while the script is running:

```text
DECISION_INBOX_PUBLISH_TOKEN=local-publisher-token \
  .venv/bin/decision-inbox publish-test --url http://127.0.0.1:8080 \
  --profile default --session local-hmr-demo
```

Run all automated checks with:

```text
./scripts/run_local_tests.sh
```

Use only the isolated development home and disposable Wiki fixture. Never point local tests at the production Hermes home, bot token, Wiki, or API keys.

## Security invariants

- Tailscale Serve is the only browser entrypoint and the server accepts only the configured tailnet owner identity.
- The app origin remains on loopback; direct-origin requests do not receive Tailscale identity headers.
- Browser sessions are short-lived signed tokens; bot and Hermes credentials never reach the browser.
- The weekly publisher is limited to Yuna/default, batched reviews, and `wiki_patch_v1` cards; the general publisher requires a recoverable source session and is configured separately.
- Every state transition is transactional and audited in SQLite WAL mode.
- Submitted manifests are immutable and SHA-256-addressed.
- Wiki paths are containment-checked; symlinks and traversal fail closed; base hashes are verified before any write; failures restore the complete target snapshot.

## Production rollout

Build a pinned wheel and service image, record their digests, create mode-600 backups, deploy the loopback service, and expose it privately with Tailscale Serve. Enable only the Yuna/default cron toolset. The exact handoff is in [docs/deployment.md](docs/deployment.md); the cron contract is in [docs/weekly-memory-wiki-review.md](docs/weekly-memory-wiki-review.md).
