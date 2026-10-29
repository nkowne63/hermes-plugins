#!/usr/bin/env bash
# Non-interactive install/update of every plugin in a pack (each pinned to its exact commit).
#   scripts/install-pack.sh packs/rpi.yaml        # uses `hermes` on PATH, or $HERMES_BIN
# `hermes plugins pack install` does the same interactively (review screen, no --yes); this runs the
# per-plugin pinned installs it fans out to. A plugin already installed from another source must be
# removed first (`hermes plugins remove <name>`); --force only reinstalls over the same source.
# Restart the gateway afterwards.
set -euo pipefail
pack="${1:?usage: scripts/install-pack.sh <pack.yaml>}"
hermes="${HERMES_BIN:-hermes}"
# Reads only the `plugins:` list entries' repo/subdir/ref scalars (stdlib only: the host python may lack PyYAML).
python3 - "$pack" <<'PY' | while IFS=$'\t' read -r url ref; do
import re, sys
entries, cur, in_plugins = [], None, False
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.split(" #", 1)[0].rstrip()
    if not line or line.lstrip().startswith("#"):
        continue
    if not line.startswith(" "):
        in_plugins = line.startswith("plugins:")
        continue
    if not in_plugins:
        continue
    m = re.match(r"\s*(-\s+)?(repo|subdir|ref|name):\s*(\S+)\s*$", line)
    if not m:
        continue
    if m.group(1):
        cur = {}
        entries.append(cur)
    cur[m.group(2)] = m.group(3).strip("'\"")
for p in entries:
    repo = p["repo"] if "://" in p["repo"] else f"https://github.com/{p['repo']}.git"
    url = f"{repo}#{p['subdir']}" if p.get("subdir") else repo
    print(f"{url}\t{p['ref']}")
PY
  echo "→ $url @ ${ref:0:12}"
  "$hermes" plugins install "$url" --ref "$ref" --force --enable --yes-deps </dev/null
done
