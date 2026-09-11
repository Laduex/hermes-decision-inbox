#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dev_home="${DECISION_INBOX_DEV_HOME:-$repo_root/.local/hermes-home}"
plugin_dir="$dev_home/plugins/hermes-decision-inbox"

mkdir -p "$dev_home/plugins" "$dev_home/skills" "$repo_root/.local/fixtures/wiki"
ln -sfn "$repo_root" "$plugin_dir"
chmod 700 "$dev_home"
printf 'Isolated Hermes home: %s\nPlugin link: %s\n' "$dev_home" "$plugin_dir"

