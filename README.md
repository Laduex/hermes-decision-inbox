# Hermes Weekly Wiki Decision Dashboard

`hermes-decision-inbox` is a standalone Hermes plugin and private web dashboard for the Weekly Memory Wiki Review. The dashboard runs continuously behind Tailscale. Every Sunday, the existing Yuna cron reviews the prior seven days and publishes one batch of exact Wiki decisions. Vaughn reviews the cards and explicitly applies the selected changes.

The cron never edits the canonical Wiki. Telegram is notification-only and sends a link to the private dashboard. The service does not consume Telegram updates and does not change Hermes core.

## Interaction model

- Right arrow or swipe right: accept the recommendation.
- Left arrow or swipe left: reject the proposal.
- Up arrow or swipe up: abstain and archive without a Wiki change.
- Down arrow or swipe down: open exact alternative answers.
- `Apply selected Wiki changes`: freeze an immutable manifest and apply only accepted recommendations or selected alternatives.

## Repository layout

- `src/hermes_decision_inbox/`: Hermes plugin, `publish_weekly_wiki_review` tool, and diagnostics CLI.
- `service/app/`: FastAPI service, SQLite repository, notification outbox, and deterministic Wiki worker.
- `web/`: keyboard- and touch-friendly web dashboard built with TypeScript and Vite.
- `skills/`: weekly-review routing and Wiki-apply instructions registered by the plugin.
- `tests/`: isolated schema, state, identity, plugin, UI-gesture, and Wiki executor tests.

The matching Hermes source worktree is `/Users/vaughndazo/Documents/Codex/worktrees/hermes-decision-inbox` on branch `feat/telegram-decision-inbox`. It remains unchanged because this feature uses the public plugin interface.

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
- The publisher is limited to Yuna/default, batched weekly reviews, and `wiki_patch_v1` cards.
- Every state transition is transactional and audited in SQLite WAL mode.
- Submitted manifests are immutable and SHA-256-addressed.
- Wiki paths are containment-checked; symlinks and traversal fail closed; base hashes are verified before any write; failures restore the complete target snapshot.

## Production rollout

Build a pinned wheel and service image, record their digests, create mode-600 backups, deploy the loopback service, and expose it privately with Tailscale Serve. Enable only the Yuna/default cron toolset. The exact handoff is in [docs/deployment.md](docs/deployment.md); the cron contract is in [docs/weekly-memory-wiki-review.md](docs/weekly-memory-wiki-review.md).
