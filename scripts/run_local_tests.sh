#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_root/.venv/bin/python"
hermes_worktree="${DECISION_INBOX_HERMES_WORKTREE:-/Users/vaughndazo/Documents/LDX/Apps/hermes-agent}"

if [[ ! -x "$python_bin" ]]; then
  echo "Create .venv and install .[test] before running validation." >&2
  exit 1
fi

cd "$repo_root"
"$python_bin" scripts/validate_package.py
"$python_bin" -m compileall -q src service
"$python_bin" -m pytest -q
"$repo_root/.venv/bin/pip" check

cd "$repo_root/web"
npm test -- --run
npm run build
npm audit --audit-level=moderate

if [[ -x "$hermes_worktree/scripts/run_tests.sh" ]]; then
  cd "$hermes_worktree"
  HERMES_PYTHON="$python_bin" \
  PYTHONPATH="$hermes_worktree:$repo_root/src" \
  scripts/run_tests.sh "$repo_root/tests/test_plugin.py" -q
else
  echo "Hermes worktree unavailable; real plugin discovery test skipped." >&2
fi
