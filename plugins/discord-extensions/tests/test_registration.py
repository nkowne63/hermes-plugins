"""Registration: the plugin supersedes the bundled ``discord`` platform and unloading restores it; private
dependencies are checked at register time; the neutralized bundled class really lacks both features."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.platforms.discord as bundled
import plugins.platforms.discord.adapter as adapter_module
from conftest import PLUGIN_DIR, ExtDiscordAdapter, RecordingContext, load_plugin_module
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter

PLUGIN_KEY = "discord-extensions"
CATEGORY = 4242


# ── private dependencies ─────────────────────────────────────────────────────────────────────


def test_every_private_dependency_exists_with_the_expected_signature(plugin_module):
    """Fails when upstream renames/re-signs a private name the subclass overrides or calls."""
    features, problems = plugin_module.check_dependencies(DiscordAdapter)
    assert problems == []
    assert features == set(plugin_module.FEATURE_DESCRIPTIONS)
    assert ExtDiscordAdapter.discord_extensions_features == features


def test_changed_dependency_disables_its_feature_with_one_warning(monkeypatch, caplog):
    async def renamed(self, msg, role_authorized=False):  # parameter list no longer matches
        return False

    monkeypatch.setattr(DiscordAdapter, "_handle_message", renamed)
    monkeypatch.delattr(DiscordAdapter, "_is_reply_reference_rejected")
    module = load_plugin_module("discord_extensions_changed_dependency")
    ctx = RecordingContext()
    with caplog.at_level(logging.WARNING):
        module.register(ctx)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "_handle_message" in text and "_is_reply_reference_rejected" in text
    for description in module.FEATURE_DESCRIPTIONS.values():
        assert description in text
    (entry,) = ctx.platforms
    assert entry["adapter_factory"].discord_extensions_features == frozenset()


def test_bundled_register_change_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(bundled, "register", lambda ctx: None)
    module = load_plugin_module("discord_extensions_register_changed")
    with caplog.at_level(logging.WARNING):
        module.register(RecordingContext())
    (warning,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "NOT overridden" in warning.getMessage()


def test_registration_is_the_bundled_one_with_only_the_adapter_replaced():
    plugin_ctx, bundled_ctx = RecordingContext(), RecordingContext()
    load_plugin_module("discord_extensions_compare").register(plugin_ctx)
    bundled.register(bundled_ctx)
    (ours,), (theirs,) = plugin_ctx.platforms, bundled_ctx.platforms
    assert theirs["adapter_factory"] is DiscordAdapter
    assert issubclass(ours["adapter_factory"], DiscordAdapter)
    assert ours["adapter_factory"] is not DiscordAdapter
    assert {k: v for k, v in ours.items() if k != "adapter_factory"} == {
        k: v for k, v in theirs.items() if k != "adapter_factory"}


# ── the bundled class (neutralized) lacks the features ───────────────────────────────────────


def _channel_message(adapter_cls, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")
    for key in ("DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_NO_THREAD_CHANNELS", "DISCORD_ALLOWED_CHANNELS",
                "DISCORD_IGNORED_CHANNELS"):
        monkeypatch.delenv(key, raising=False)
    adapter = adapter_cls(PlatformConfig(enabled=True, token="fake-token"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=1))
    adapter.handle_message = AsyncMock()
    adapter._text_batch_delay_seconds = 0
    adapter._auto_create_thread = AsyncMock(return_value=None)
    adapter.config.extra["category_defaults"] = {str(CATEGORY): {"require_mention": False, "thread_response": False}}
    channel = SimpleNamespace(id=10, name="c", category_id=CATEGORY, topic=None,
                              guild=SimpleNamespace(id=7, name="g"))
    message = SimpleNamespace(
        id=11, content="hi", mentions=[], attachments=[], reference=None, created_at=datetime.now(UTC),
        channel=channel, author=SimpleNamespace(id=2, display_name="u", name="u"), guild=channel.guild)
    return adapter, message


@pytest.mark.asyncio
async def test_neutralized_bundled_class_ignores_scoped_defaults(monkeypatch):
    adapter, message = _channel_message(DiscordAdapter, monkeypatch)
    assert await adapter._handle_message(message) is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_class_applies_scoped_defaults(monkeypatch):
    adapter, message = _channel_message(ExtDiscordAdapter, monkeypatch)
    assert await adapter._handle_message(message) is True
    adapter.handle_message.assert_awaited_once()
    adapter._auto_create_thread.assert_not_awaited()  # thread_response: false


def test_neutralized_bundled_class_keeps_starter_reference_on_the_thread(monkeypatch):
    monkeypatch.setattr(adapter_module.discord, "MessageReference", lambda **kw: SimpleNamespace(**kw))
    thread = SimpleNamespace(id=555, parent_id=111, parent=SimpleNamespace(id=111, type=0),
                             guild=SimpleNamespace(id=9))
    bare = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    ours = ExtDiscordAdapter(PlatformConfig(enabled=True, token="***"))
    assert bare._message_reference_from_ids("555", thread).channel_id == 555
    assert ours._message_reference_from_ids("555", thread).channel_id == 111
    err = RuntimeError("400 Bad Request (error code: 50035): Invalid Form Body\nIn message_reference: Unknown message")
    assert DiscordAdapter._is_reply_reference_rejected(err) is False
    assert ExtDiscordAdapter._is_reply_reference_rejected(err) is True


def test_helpers_are_unchanged_outside_an_ingress_scope(monkeypatch):
    """Backfill and any other caller outside the three ingress methods see the plain config."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
    adapter = ExtDiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter.config.extra["guild_defaults"] = {"7": {"require_mention": False, "thread_response": False}}
    assert adapter._discord_require_mention() is True
    assert adapter._discord_free_response_channels() == set()
    assert adapter._get_no_thread_channels() == set()


# ── plugin manager: supersede and restore ───────────────────────────────────────────────────


@pytest.fixture
def hermes_home(isolated_hermes_home):
    (isolated_hermes_home / "plugins").mkdir()
    (isolated_hermes_home / "plugins" / PLUGIN_KEY).symlink_to(PLUGIN_DIR, target_is_directory=True)
    return isolated_hermes_home


def _discover(home, enabled):
    (home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": enabled}}))  # JSON is valid YAML
    from hermes_cli.plugins import _ensure_plugins_discovered
    return _ensure_plugins_discovered(force=True)


def _discord_factory():
    from gateway.platform_registry import platform_registry
    entry = platform_registry.get("discord")
    assert entry is not None
    factory = entry.adapter_factory
    return factory, (factory.__module__, factory.__qualname__)


def test_enabled_plugin_supersedes_bundled_discord_and_unload_restores_it(hermes_home):
    # The bundled platform resolves through its deferred loader, imported as hermes_plugins.platforms__discord.
    _discover(hermes_home, [])
    _, bundled_identity = _discord_factory()
    assert bundled_identity == ("hermes_plugins.platforms__discord.adapter", "DiscordAdapter")
    try:
        manager = _discover(hermes_home, [PLUGIN_KEY])
        loaded = manager._plugins.get(PLUGIN_KEY)
        assert loaded is not None and loaded.enabled and not loaded.error, f"plugin not loaded: {loaded!r}"
        factory, identity = _discord_factory()
        assert identity == ("hermes_plugins.discord_extensions", "DiscordExtensionsAdapter")
        assert issubclass(factory, DiscordAdapter)
        assert factory.discord_extensions_features == {"scoped_defaults", "starter_reply_reference"}

        assert manager.unload(PLUGIN_KEY) is True
        assert _discord_factory()[1] == bundled_identity
    finally:
        _discover(hermes_home, [])
    assert _discord_factory()[1] == bundled_identity
