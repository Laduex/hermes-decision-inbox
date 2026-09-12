#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_root/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  echo "Create the local environment first: .venv/bin/pip install -e '.[test]'" >&2
  exit 1
fi
if [[ ! -d "$repo_root/web/node_modules" ]]; then
  echo "Install the frontend dependencies first: (cd web && npm ci)" >&2
  exit 1
fi

export DECISION_INBOX_STATE_DIR="${DECISION_INBOX_STATE_DIR:-$repo_root/.local/state}"
export DECISION_INBOX_DATABASE="${DECISION_INBOX_DATABASE:-$DECISION_INBOX_STATE_DIR/decision-inbox.sqlite3}"
export DECISION_INBOX_MANIFEST_ROOT="${DECISION_INBOX_MANIFEST_ROOT:-$DECISION_INBOX_STATE_DIR/manifests}"
export DECISION_INBOX_BACKUP_ROOT="${DECISION_INBOX_BACKUP_ROOT:-$DECISION_INBOX_STATE_DIR/backups}"
export DECISION_INBOX_WIKI_ROOT="${DECISION_INBOX_WIKI_ROOT:-$repo_root/.local/fixtures/wiki}"
export DECISION_INBOX_SIGNING_KEY="${DECISION_INBOX_SIGNING_KEY:-local-dashboard-signing-key-change-me}"
export DECISION_INBOX_PUBLISH_TOKEN="${DECISION_INBOX_PUBLISH_TOKEN:-local-publisher-token}"
export DECISION_INBOX_PUBLISH_TOKENS="${DECISION_INBOX_PUBLISH_TOKENS:-{\"default\":\"$DECISION_INBOX_PUBLISH_TOKEN\"}}"
export DECISION_INBOX_TAILSCALE_OWNER_LOGIN="${DECISION_INBOX_TAILSCALE_OWNER_LOGIN:-local-owner}"
export VITE_TAILSCALE_OWNER_LOGIN="$DECISION_INBOX_TAILSCALE_OWNER_LOGIN"
export DECISION_INBOX_MINI_APP_URL="${DECISION_INBOX_MINI_APP_URL:-http://127.0.0.1:5173}"
export DECISION_INBOX_WORKER_ENABLED="${DECISION_INBOX_WORKER_ENABLED:-false}"
export DECISION_INBOX_AUTO_RESUME="${DECISION_INBOX_AUTO_RESUME:-false}"
export DECISION_INBOX_WIKI_EXECUTOR_ENABLED="${DECISION_INBOX_WIKI_EXECUTOR_ENABLED:-true}"
export DECISION_INBOX_WEEKLY_ONLY="${DECISION_INBOX_WEEKLY_ONLY:-false}"
export DECISION_INBOX_DEV_SERVICE_URL="${DECISION_INBOX_DEV_SERVICE_URL:-http://127.0.0.1:8080}"

mkdir -p "$DECISION_INBOX_STATE_DIR" "$DECISION_INBOX_WIKI_ROOT"
service_log="$repo_root/.local/decision-inbox-service.log"
service_pid=""
if ! curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
  "$python_bin" -m uvicorn service.app.main:app --host 127.0.0.1 --port 8080 >"$service_log" 2>&1 &
  service_pid=$!
fi
cleanup() {
  if [[ -n "$service_pid" ]]; then
    kill "$service_pid" 2>/dev/null || true
    wait "$service_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then break; fi
  sleep 0.2
done
if ! curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
  echo "Local service failed to start; see $service_log" >&2
  exit 1
fi

echo "Local service: http://127.0.0.1:8080 (isolated state under $repo_root/.local)"
echo "HMR dashboard: http://127.0.0.1:5173"
echo "Press Ctrl-C to stop both processes."
cd "$repo_root/web"
npm run dev
