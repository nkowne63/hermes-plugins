"""Load the plugin the way a user install does, before the test module imports it.

``hermes plugins install …#plugins/devin`` clones the plugin flat into ``$HERMES_HOME/plugins/devin/``; provider
discovery imports any child there whose manifest says ``kind: model-provider`` into the ``$HERMES_HOME`` layer
of the registry, under a per-home module name. The suite reproduces exactly that: a scratch ``HERMES_HOME`` with
``plugins/devin`` -> this plugin directory, bound for the whole session, so every lookup (``get_provider_profile``,
the auth registry, ``AIAgent``) goes through the real user-plugin path, not the bundled one. The Hermes tree is
``~/.hermes/hermes-agent`` (``scripts/test-plugin.sh`` points HOME at a scratch home holding the pinned tree).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"


def install_into(home: Path) -> Path:
    """Lay the plugin out under ``home`` the way ``hermes plugins install`` does (flat ``plugins/<name>/``)."""
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    target = home / "plugins" / "devin"
    if not target.exists():
        target.symlink_to(PLUGIN_DIR, target_is_directory=True)
    return home


_SESSION_HOME = install_into(Path(tempfile.mkdtemp(prefix="devin-plugin-home-")))
os.environ["HERMES_HOME"] = str(_SESSION_HOME)
for var in ("DEVIN_API_KEY", "DEVIN_BASE_URL"):
    os.environ.pop(var, None)
if str(HERMES_TREE) not in sys.path:
    sys.path.insert(0, str(HERMES_TREE))


def pytest_unconfigure(config):
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)
