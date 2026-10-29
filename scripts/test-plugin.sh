#!/usr/bin/env bash
# Run one plugin's tests against the Hermes tree pinned in hermes-ref.env.
#   scripts/test-plugin.sh <plugin-dir-name> [pytest args...]
# The tests find Hermes at ~/.hermes/hermes-agent (convention of the session-reset-policy suite), so
# this script runs them under a scratch HOME whose .hermes/hermes-agent is a cached checkout of the
# pinned ref. Python deps: the plugin's requirements-dev.txt plus the Hermes tree's core deps.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
name="${1:?usage: scripts/test-plugin.sh <plugin> [pytest args...]}"; shift
plugin="$root/plugins/$name"
[ -d "$plugin/tests" ] || { echo "no tests in $plugin" >&2; exit 2; }
# shellcheck disable=SC1091
. "$root/hermes-ref.env"
cache="${HERMES_PLUGINS_CACHE:-$HOME/.cache/hermes-plugins}"
tree="$cache/hermes-agent-$HERMES_REF"
if [ ! -d "$tree/.git" ]; then
  mkdir -p "$cache"
  git clone -q --filter=blob:none --no-checkout "$HERMES_REPO" "$tree"
  git -C "$tree" checkout -q "$HERMES_REF"
fi
venv="$cache/venv-$HERMES_REF"
if [ ! -f "$venv/.deps-installed" ]; then
  rm -rf "$venv"
  uv venv -q --python 3.14 "$venv"
  # The tree's uv.lock is the complete, already-resolved set (its overrides included), so install it as is
  # (--no-deps); the Hermes package itself is not installed: the tests import it from the tree.
  uv export -q --frozen --no-hashes --no-emit-project --project "$tree" > "$cache/requirements-$HERMES_REF.txt"
  uv pip install -q --no-deps -p "$venv/bin/python" -r "$cache/requirements-$HERMES_REF.txt"
  touch "$venv/.deps-installed"
fi
[ -f "$plugin/requirements-dev.txt" ] && uv pip install -q -p "$venv/bin/python" -r "$plugin/requirements-dev.txt"
home="$cache/home-$HERMES_REF"
mkdir -p "$home/.hermes"
ln -sfn "$tree" "$home/.hermes/hermes-agent"
cd "$plugin"
# HERMES_DISABLE_LAZY_INSTALLS: importing gateway modules otherwise lets hermes_bootstrap re-exec the interpreter
# into another environment, which silently kills the pytest process mid-run.
HOME="$home" HERMES_DISABLE_LAZY_INSTALLS=1 exec "$venv/bin/python" -m pytest tests -q "$@"
