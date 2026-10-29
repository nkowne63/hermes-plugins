"""Tests for the hermes-thread-only-display plugin (port of fork commit 5327a5da17).

The pinned Hermes tree still carries the fork's core implementation; every test neutralizes it by
replacing ``gateway.display_config.is_thread_only_display_channel`` (which the core imports lazily at
each call) with a constant False, so only the plugin can suppress anything.
``test_core_is_neutralized`` proves that without the plugin nothing is suppressed.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"

PARENT = "1509077323490791515"
# gateway.run imports hermes_bootstrap, which may re-exec the interpreter into a managed venv (losing
# pytest) when the cached tree carries a self-update install stamp. Tests must stay in this process.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
THREAD = "1533727118754840576"

DISPLAY = {
    "tool_progress": "all",
    "interim_assistant_messages": True,
    "thinking_progress": True,
    "show_reasoning": True,
    "thread_only_channels": [PARENT],
}


@pytest.fixture(autouse=True)
def hermes_tree(monkeypatch):
    if not HERMES_TREE.is_dir():
        pytest.skip(f"Hermes tree not found at {HERMES_TREE}")
    if str(HERMES_TREE) not in sys.path:
        sys.path.insert(0, str(HERMES_TREE))
    import gateway.display_config as display_config
    # Neutralize the fork's core implementation (absent upstream: raising=False).
    monkeypatch.setattr(display_config, "is_thread_only_display_channel",
                        lambda *_a, **_k: False, raising=False)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_thread_only_display", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = SimpleNamespace(hooks={}, unload=[])
    ctx.register_hook = lambda name, cb: ctx.hooks.setdefault(name, []).append(cb)
    ctx.on_unload = ctx.unload.append
    module.ctx = ctx
    return module


@pytest.fixture
def plugin():
    import gateway.run  # noqa: F401  (a gateway process: the wraps install at register)
    module = _load_plugin()
    module.register(module.ctx)
    yield module
    for callback in module.ctx.unload:
        callback()


def _source(*, chat_id=PARENT, thread_id=None, chat_type="group"):
    from gateway.config import Platform
    from gateway.session import SessionSource
    return SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type=chat_type, thread_id=thread_id)


# ── matcher ──────────────────────────────────────────────────────────

_THREAD_KW = {"chat_id": THREAD, "thread_id": THREAD, "chat_type": "thread"}


@pytest.mark.parametrize(
    ("configured", "source_kw", "expected"),
    [
        ([PARENT], {}, True),
        ([int(PARENT)], {}, True),
        (f"{PARENT}, 42", {}, True),
        (int(PARENT), {}, True),
        ([PARENT], {"chat_id": "other"}, False),
        # Discord threads carry thread_id (chat_id may be the thread or the parent).
        ([PARENT], _THREAD_KW, False),
        ([PARENT], {"chat_id": PARENT, "thread_id": THREAD, "chat_type": "thread"}, False),
        ([], {}, False),
        (None, {}, False),
    ],
)
def test_is_thread_only_display_channel(configured, source_kw, expected):
    match = _load_plugin().is_thread_only_display_channel
    config = {"display": {"thread_only_channels": configured}}
    assert match(config, _source(**source_kw)) is expected


def test_is_thread_only_display_channel_tolerates_missing_display():
    match = _load_plugin().is_thread_only_display_channel
    assert match({}, _source()) is False
    assert match({"display": "bad"}, _source()) is False
    assert match(None, _source()) is False


def test_source_without_thread_attributes_is_not_suppressed():
    from gateway.config import Platform
    match = _load_plugin().is_thread_only_display_channel
    assert match({"display": {"thread_only_channels": [PARENT]}},
                 SimpleNamespace(platform=Platform.DISCORD)) is False


# ── per-turn display settings ────────────────────────────────────────


def _display_settings(monkeypatch, tmp_path, source, display):
    import hermes_yaml as yaml
    from gateway.config import Platform
    from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"display": display}), encoding="utf-8")
    monkeypatch.delenv("HERMES_TOOL_PROGRESS_MODE", raising=False)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = _make_runner(ProgressCaptureAdapter(platform=Platform.DISCORD))
    return runner._run_agent_display_settings(source)


def test_core_is_neutralized(monkeypatch, tmp_path):
    disp = _display_settings(monkeypatch, tmp_path, _source(), DISPLAY)
    assert disp.tool_progress_enabled is True
    assert disp.interim_assistant_messages_enabled is True
    assert disp._thinking_enabled is True


def test_parent_channel_suppresses_progress_interim_and_thinking(plugin, monkeypatch, tmp_path):
    disp = _display_settings(monkeypatch, tmp_path, _source(), DISPLAY)

    assert disp.progress_mode == "off"
    assert disp.tool_progress_enabled is False
    assert disp.interim_assistant_messages_enabled is False
    assert disp._thinking_enabled is False
    assert disp.needs_progress_queue is False


def test_thread_under_parent_keeps_display(plugin, monkeypatch, tmp_path):
    disp = _display_settings(monkeypatch, tmp_path, _source(**_THREAD_KW), DISPLAY)

    assert disp.progress_mode == "all"
    assert disp.tool_progress_enabled is True
    assert disp.interim_assistant_messages_enabled is True
    assert disp._thinking_enabled is True


def test_unlisted_channel_keeps_display(plugin, monkeypatch, tmp_path):
    disp = _display_settings(monkeypatch, tmp_path, _source(chat_id="other"), DISPLAY)

    assert disp.tool_progress_enabled is True
    assert disp.interim_assistant_messages_enabled is True


def test_log_mode_is_not_chat_output_and_is_kept(plugin, monkeypatch, tmp_path):
    disp = _display_settings(monkeypatch, tmp_path, _source(), {**DISPLAY, "tool_progress": "log"})

    assert disp.progress_mode == "log"
    assert disp.log_mode_enabled is True
    assert disp.log_queue is not None
    assert disp.tool_progress_enabled is False


# ── end to end through _run_agent ────────────────────────────────────


class ChattyAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        if self.interim_assistant_callback:
            self.interim_assistant_callback("Let me look around first.", already_streamed=False)
        if self.tool_progress_callback:
            self.tool_progress_callback("tool.started", "terminal", "pwd", {})
            time.sleep(0.35)
        return {"final_response": "done", "messages": [], "api_calls": 1}


async def _run(monkeypatch, tmp_path, *, chat_id, thread_id, chat_type):
    from gateway.config import Platform
    from tests.gateway.test_run_progress_topics import _run_with_agent
    return await _run_with_agent(
        monkeypatch, tmp_path, ChattyAgent,
        session_id=f"sess-thread-only-{thread_id or 'parent'}",
        config_data={"display": DISPLAY, "streaming": {"enabled": False}},
        platform=Platform.DISCORD, chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_parent_channel_turn_sends_only_the_final_answer(plugin, monkeypatch, tmp_path):
    adapter, result = await _run(monkeypatch, tmp_path, chat_id=PARENT, thread_id=None, chat_type="group")

    assert result["final_response"] == "done"
    assert adapter.sent == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_thread_turn_still_shows_progress_and_interim(plugin, monkeypatch, tmp_path):
    adapter, _ = await _run(monkeypatch, tmp_path, chat_id=THREAD, thread_id=THREAD, chat_type="thread")

    sent = [call["content"] for call in adapter.sent]
    assert "Let me look around first." in sent
    assert any("pwd" in text for text in sent + [edit["content"] for edit in adapter.edits])


# ── final-response reasoning ─────────────────────────────────────────


def _prepend_all(monkeypatch):
    import gateway.run as gateway_run
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"display": DISPLAY})
    result = {"last_reasoning": "private chain"}
    runner = GatewayTurnPersistenceMixin()
    return [runner._hmwa_prepend_reasoning(result, "final answer", source, False) for source in (
        _source(), _source(**_THREAD_KW), _source(chat_id="other"))]


def test_core_reasoning_is_neutralized(monkeypatch):
    parent, _thread, _other = _prepend_all(monkeypatch)
    assert "private chain" in parent


def test_prepended_reasoning_is_thread_only(plugin, monkeypatch):
    parent, thread, other = _prepend_all(monkeypatch)

    assert parent == "final answer"
    assert "private chain" in thread and thread.endswith("final answer")
    assert "private chain" in other


# ── install / private-API guards ─────────────────────────────────────


def test_wraps_are_on_the_expected_private_seams(plugin):
    """Fails when upstream renames/re-signs a seam or the _RunAgentDisplay fields."""
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
    assert getattr(GatewayTurnMixin.__dict__["_run_agent_display_settings"], plugin._WRAPPED_MARK)
    assert getattr(GatewayTurnPersistenceMixin.__dict__["_hmwa_prepend_reasoning"], plugin._WRAPPED_MARK)
    assert plugin._check_display_dataclass() is None
    assert plugin._warned == set()


def test_unload_restores_originals(monkeypatch):
    import gateway.run  # noqa: F401
    from gateway.run_turn import GatewayTurnMixin
    original = GatewayTurnMixin.__dict__["_run_agent_display_settings"]
    module = _load_plugin()
    module.register(module.ctx)
    assert GatewayTurnMixin.__dict__["_run_agent_display_settings"] is not original
    module.register(module.ctx)  # idempotent: no double wrap
    assert getattr(GatewayTurnMixin.__dict__["_run_agent_display_settings"], module._WRAPPED_MARK) is original
    for callback in module.ctx.unload:
        callback()
    assert GatewayTurnMixin.__dict__["_run_agent_display_settings"] is original


@pytest.mark.asyncio
async def test_install_is_deferred_until_gateway_loaded(monkeypatch):
    import gateway.run as gateway_run
    from gateway.run_turn import GatewayTurnMixin
    original = GatewayTurnMixin.__dict__["_run_agent_display_settings"]
    monkeypatch.delitem(sys.modules, "gateway.run")
    module = _load_plugin()
    module.register(module.ctx)
    assert GatewayTurnMixin.__dict__["_run_agent_display_settings"] is original
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    try:
        await module.ctx.hooks["pre_gateway_dispatch"][0](event=None, gateway=None, session_store=None)
        assert getattr(GatewayTurnMixin.__dict__["_run_agent_display_settings"], module._WRAPPED_MARK) is original
    finally:
        for callback in module.ctx.unload:
            callback()


def test_renamed_seams_warn_once_and_leave_core_untouched(monkeypatch, caplog):
    import gateway.run  # noqa: F401
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
    original = GatewayTurnMixin.__dict__["_run_agent_display_settings"]
    monkeypatch.delattr(GatewayTurnPersistenceMixin, "_hmwa_prepend_reasoning")
    monkeypatch.setattr(GatewayTurnMixin, "_run_agent_display_settings", lambda self, source, extra: None)
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
        module.install()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert any("_hmwa_prepend_reasoning is missing" in w and "show_reasoning" in w for w in warnings)
    assert any("signature changed" in w and "tool progress" in w for w in warnings)
    assert not hasattr(GatewayTurnMixin.__dict__["_run_agent_display_settings"], module._WRAPPED_MARK)
    for callback in module.ctx.unload:
        callback()
    assert original is not None


def test_display_dataclass_change_warns_and_unwraps(monkeypatch, caplog):
    import dataclasses
    import gateway.run as gateway_run
    from gateway.run_turn import GatewayTurnMixin

    @dataclasses.dataclass
    class Renamed:
        progress_mode: object = None

    monkeypatch.setattr(gateway_run.GatewayRunner, "_RunAgentDisplay", Renamed)
    original = GatewayTurnMixin.__dict__["_run_agent_display_settings"]
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "_RunAgentDisplay lacks fields" in warnings[0]
    assert GatewayTurnMixin.__dict__["_run_agent_display_settings"] is original
    for callback in module.ctx.unload:
        callback()
