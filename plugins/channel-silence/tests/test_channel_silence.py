"""Tests for the hermes-channel-silence plugin (port of fork commit c7d91aef35).

Standalone-suite discipline: collection imports nothing from Hermes; the real adapter/runner classes
are imported lazily from ~/.hermes/hermes-agent. The adapters are built WITHOUT core channel
overrides, so on a tree that still carries the fork's core stamp only the plugin can set
``reply_expected=False`` — the config lives in the profile's config.yaml, as on the rpi.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"

CONFIG = """\
discord:
  channel_overrides:
    "quiet":
      allow_silence: true
    "loud":
      allow_silence: false
    "modelonly":
      model: some/model
    "thread_shadow":
      model: some/model
"""


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    if not HERMES_TREE.is_dir():
        pytest.skip(f"Hermes tree not found at {HERMES_TREE}")
    if str(HERMES_TREE) not in sys.path:
        sys.path.insert(0, str(HERMES_TREE))
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", f"/pytest-{tmp_path.name}")
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    monkeypatch.setattr(hermes_constants, "_profile_fallback_warned", True, raising=False)
    return home


@pytest.fixture
def plugin():
    """The plugin registered against a recording ctx; unloaded (wrapper removed) afterwards."""
    import gateway.platforms.base  # noqa: F401 -- loaded first, as in a gateway process
    spec = importlib.util.spec_from_file_location("hermes_channel_silence", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = SimpleNamespace(hooks={}, unload=[])
    ctx.register_hook = lambda name, cb: ctx.hooks.setdefault(name, []).append(cb)
    ctx.on_unload = ctx.unload.append
    module.register(ctx)
    module.ctx = ctx
    yield module
    for callback in ctx.unload:
        callback()


def _adapter_cls():
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class _DiscordLikeAdapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect: bool = False):
            return True

        async def disconnect(self):
            pass

        async def get_chat_info(self, chat_id):
            return None

        async def send(self, *args, **kwargs):
            return SendResult(success=True, message_id="x")

    return _DiscordLikeAdapter


def _adapter():
    from gateway.config import Platform, PlatformConfig
    adapter = _adapter_cls()(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
    adapter.set_message_handler(AsyncMock(return_value=None))
    started = []
    adapter._start_session_processing = lambda event, key: started.append(event) or True
    return adapter, started


def _event(chat_id="quiet", *, thread_id=None, parent_chat_id=None, internal=False,
           reply_expected=None, text="side chatter"):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource
    source = SessionSource(
        platform=Platform.DISCORD, chat_id=chat_id, chat_type="thread" if thread_id else "group",
        user_id="u1", thread_id=thread_id, parent_chat_id=parent_chat_id,
    )
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=source, message_id="m1",
        internal=internal, reply_expected=reply_expected,
    )


def _admit(event):
    adapter, started = _adapter()
    _run(adapter.handle_message(event))
    assert started == [event]
    return event


# ── adapter ingress (fork parity: BasePlatformAdapter.handle_message) ────────────────────────────

def test_human_turn_in_allow_silence_channel_is_unaddressed(plugin):
    assert _admit(_event(reply_expected=True)).reply_expected is False


def test_thread_inherits_parent_allow_silence(plugin):
    event = _admit(_event("thread_9", thread_id="thread_9", parent_chat_id="quiet"))
    assert event.reply_expected is False


@pytest.mark.parametrize("chat_id", ["loud", "modelonly", "elsewhere"],
                         ids=["explicit-false", "override-without-key", "other-channel"])
def test_other_channels_keep_reply_expected(plugin, chat_id):
    assert _admit(_event(chat_id)).reply_expected is None


def test_no_overrides_configured(plugin, hermes_home):
    (hermes_home / "config.yaml").write_text("model: x\n")
    assert _admit(_event()).reply_expected is None


def test_thread_override_shadows_parent_like_core_lookup(plugin):
    """First existing override of chat → thread → parent wins (``_channel_override_lookup_keys``)."""
    event = _admit(_event("thread_shadow", thread_id="thread_shadow", parent_chat_id="quiet"))
    assert event.reply_expected is None


def test_internal_turn_keeps_its_own_verdict(plugin):
    assert _admit(_event(internal=True)).reply_expected is None


def test_nested_platforms_section_is_read(plugin, hermes_home):
    (hermes_home / "config.yaml").write_text(
        'platforms:\n  discord:\n    channel_overrides:\n      "quiet":\n        allow_silence: "true"\n')
    assert _admit(_event()).reply_expected is False


# ── busy / queued follow-ups (never reach pre_gateway_dispatch) ──────────────────────────────────

def _busy_adapter():
    adapter, started = _adapter()
    session_key = adapter._event_session_key(_event())
    adapter._active_sessions[session_key] = object()
    adapter._heal_stale_session_lock = lambda _key: None
    return adapter, started, session_key


def test_busy_follow_up_is_stamped_before_it_is_queued(plugin):
    """A message arriving mid-turn lands in the pending slot already carrying the verdict, so a
    queued chain's terminal turn (``queued_terminal_reply_expected``) honors it too."""
    adapter, _started, _key = _busy_adapter()
    queued = []
    adapter._handle_message_while_active = AsyncMock(side_effect=lambda ev, key: queued.append(ev))
    event = _event()
    _run(adapter.handle_message(event))
    assert queued == [event] and event.reply_expected is False


def test_busy_session_handler_sees_stamped_event(plugin):
    """The runner's busy handler (queue / steer fold / interrupt) gets the stamped event, so
    ``absorb_reply_expected`` into the running turn keeps it unaddressed."""
    adapter, _started, _key = _busy_adapter()
    seen = []

    async def busy_handler(event, session_key):
        seen.append(event.reply_expected)
        return True

    adapter._busy_session_handler = busy_handler
    _run(adapter.handle_message(_event()))
    assert seen == [False]


def test_real_pending_slot_merge_keeps_verdict(plugin):
    """Two follow-ups merged into the adapter's own pending slot (no runner) stay unaddressed."""
    adapter, _started, key = _busy_adapter()
    adapter._is_queue_text_debounce_candidate = lambda _event: False

    async def two():
        await adapter.handle_message(_event(text="one"))
        await adapter.handle_message(_event(text="two"))

    _run(two())
    pending = adapter._pending_messages[key]
    assert "one" in pending.text and "two" in pending.text
    assert pending.reply_expected is False


def test_text_batch_merge_keeps_verdict(plugin):
    """Chunks merged by ``_enqueue_text_event`` (before ``handle_message``) are stamped too."""
    adapter, _started = _adapter()

    async def enqueue():
        adapter._enqueue_text_event(_event(text="a"))
        adapter._enqueue_text_event(_event(text="b"))
        batch = dict(adapter._pending_text_batches)
        for task in list(adapter._pending_text_batch_tasks.values()):
            task.cancel()
        return batch

    (batch,) = _run(enqueue()).values()
    assert batch.reply_expected is False


# ── pre_gateway_dispatch hook ────────────────────────────────────────────────────────────────────

def test_pre_gateway_dispatch_stamps_and_allows(plugin):
    (hook,) = plugin.ctx.hooks["pre_gateway_dispatch"]
    quiet, loud, internal = _event(), _event("loud"), _event(internal=True)
    for event in (quiet, loud, internal):
        assert _run(hook(event=event, gateway=None, session_store=None)) is None
    assert (quiet.reply_expected, loud.reply_expected, internal.reply_expected) == (False, None, None)


def test_pre_gateway_dispatch_reads_the_routed_profile(plugin, hermes_home, monkeypatch):
    profile_home = hermes_home / "profiles" / "quietbot"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        'discord:\n  channel_overrides:\n    "loud":\n      allow_silence: true\n')
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: hermes_home / "profiles" / name)
    (hook,) = plugin.ctx.hooks["pre_gateway_dispatch"]
    event = _event("loud")
    event.source.profile = "quietbot"
    _run(hook(event=event, gateway=None, session_store=None))
    assert event.reply_expected is False


def test_unload_restores_adapter_gate(plugin):
    from gateway.platforms.base import BasePlatformAdapter
    assert getattr(BasePlatformAdapter._drop_unresolved, "_channel_silence_wrapped", None)
    for callback in plugin.ctx.unload:
        callback()
    plugin.ctx.unload.clear()
    assert not getattr(BasePlatformAdapter._drop_unresolved, "_channel_silence_wrapped", None)
    assert _admit(_event()).reply_expected is None


# ── end to end: no ⚠️ fallback, crash-recovery metadata ──────────────────────────────────────────

def _runner(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionEntry
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:discord:group:quiet", session_id="sess-silent",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.DISCORD, chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_a, **_kw: 100_000)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "[SILENT]",
        "messages": [{"role": "user", "content": "side chatter"},
                     {"role": "assistant", "content": "[SILENT]"}],
        "tools": [], "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 1, "failed": False,
    })
    return runner


@pytest.mark.parametrize("chat_id,silent", [("quiet", True), ("loud", False)])
def test_discord_human_turn_silence_end_to_end(plugin, monkeypatch, tmp_path, chat_id, silent):
    """Admitted through the adapter, then run: the opted-in channel stays quiet (no ⚠️ fallback) and
    the persisted user row records ``reply_expected: false`` for crash recovery."""
    from gateway.response_filters import is_intentional_silence_response
    event = _admit(_event(chat_id))
    runner = _runner(monkeypatch, tmp_path)
    response = _run(runner._handle_message_with_agent(
        event, event.source, f"agent:main:discord:group:{chat_id}", 1))
    if silent:
        assert response == ""
        metadata = runner._run_agent.await_args.kwargs["persist_user_display_metadata"]
        assert metadata.get("reply_expected") is False
    else:
        assert response and not is_intentional_silence_response(response)
