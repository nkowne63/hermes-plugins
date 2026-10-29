"""Shared setup: the Hermes tree on sys.path, the plugin's adapter class, and the core fork code neutralized.

The pinned Hermes tree is the fork main, which still carries fork commits 57d2d5b2c2 and e8e420baae in the
bundled ``DiscordAdapter``. ``neutralize_core`` (autouse) puts the bundled class back to upstream behavior
for the duration of each test, so every assertion that passes does so because of the plugin's subclass;
``test_registration.py`` checks that the neutralized bundled class really lacks both features.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"

if not HERMES_TREE.is_dir():
    pytest.exit(f"Hermes tree not found at {HERMES_TREE}", returncode=2)
if str(HERMES_TREE) not in sys.path:
    sys.path.insert(0, str(HERMES_TREE))


def load_plugin_module(name: str = "discord_extensions_under_test"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RecordingContext:
    def __init__(self):
        self.platforms = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


def register_with_recording_ctx(module):
    ctx = RecordingContext()
    module.register(ctx)
    return ctx


_PLUGIN = load_plugin_module()
_CTX = register_with_recording_ctx(_PLUGIN)
ExtDiscordAdapter = next(p["adapter_factory"] for p in _CTX.platforms if p["name"] == "discord")


def _upstream_is_reply_reference_rejected(err):
    """v0.22.5's classifier (before e8e420baae)."""
    err_text = str(err)
    return (
        "error code: 50035" in err_text and "Cannot reply to a system message" in err_text
    ) or "error code: 10008" in err_text


@pytest.fixture(autouse=True)
def neutralize_core(monkeypatch):
    """Bundled DiscordAdapter as upstream v0.22.5: no scoped defaults, no starter reference rewrite."""
    from plugins.platforms.discord.adapter import DiscordAdapter

    # 57d2d5b2c2: no scoped rule ever applies, no category lookup (no_thread_channels by channel keys only).
    monkeypatch.setattr(DiscordAdapter, "_discord_scoped_flag", lambda self, message, key: None, raising=False)
    monkeypatch.setattr(DiscordAdapter, "_get_category_id", lambda self, channel: None, raising=False)
    # e8e420baae: the reference names the send target; the classifier is upstream's.
    monkeypatch.setattr(
        DiscordAdapter, "_reply_reference_channel_id",
        lambda self, message_id, channel: getattr(channel, "id", None), raising=False)
    monkeypatch.setattr(
        DiscordAdapter, "_is_reply_reference_rejected", staticmethod(_upstream_is_reply_reference_rejected))


@pytest.fixture
def ext_adapter_cls():
    return ExtDiscordAdapter


@pytest.fixture(autouse=True)
def isolated_hermes_home(tmp_path, monkeypatch):
    """Per-test HERMES_HOME: the adapter persists joined threads (discord_threads.json) there."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", f"/pytest-{tmp_path.name}")
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    return home


@pytest.fixture
def plugin_module():
    return _PLUGIN
