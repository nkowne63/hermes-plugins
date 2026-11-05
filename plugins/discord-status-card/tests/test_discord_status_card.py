"""Tests for the discord-status-card plugin, against the Hermes tree pinned in hermes-ref.env.

Turns run through the real ``GatewayRunner._run_agent`` (the Hermes test harness builds the runner), with a
``DiscordAdapter`` subclass whose channel, message and text-send I/O is captured. The fake agents fire what a
real turn produces: the ID-correlated tool callbacks the core task-card rail consumes, the text
``tool_progress_callback`` / ``interim_assistant_callback`` the core would render as messages, and the plugin
hooks Hermes invokes (``post_api_request``, ``post_tool_call``, ``on_interim_message``, ``on_session_end``,
``agent_loop_stopped``), called from the agent thread with the turn's session context like the real dispatch.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
import time
import types
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"
# gateway.run imports hermes_bootstrap, which may re-exec the interpreter; tests must stay in this process.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")

CONSOLE = "1501181227359539401"  # the listed parent channel
THREAD = "1533727118754840576"  # a thread under it
OTHER_PARENT = "1509077323490791515"
OTHER_THREAD = "1538000000000000001"
REQUESTER = "111"
ALLOWED = "222"
STRANGER = "333"

DISPLAY = {
    "tool_progress": "all",
    "interim_assistant_messages": True,
    "status_card_channels": [CONSOLE],
}

HOOKS: dict = {}  # hook name -> callbacks of the registered plugin (what invoke_hook would call)


@pytest.fixture(autouse=True)
def hermes_tree(monkeypatch, tmp_path):
    if not HERMES_TREE.is_dir():
        pytest.skip(f"Hermes tree not found at {HERMES_TREE}")
    if str(HERMES_TREE) not in sys.path:
        sys.path.insert(0, str(HERMES_TREE))
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("DISCORD_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS",
                 "DISCORD_ALLOWED_USERS", "HERMES_TOOL_PROGRESS_MODE", "HERMES_AGENT_NOTIFY_INTERVAL"):
        monkeypatch.delenv(name, raising=False)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = SimpleNamespace(hooks={}, unload=[])
    ctx.register_hook = lambda hook, cb: ctx.hooks.setdefault(hook, []).append(cb)
    ctx.on_unload = ctx.unload.append
    module.ctx = ctx
    return module


def _load_plugin():
    return _load_module(PLUGIN_DIR / "__init__.py", "discord_status_card_under_test")


def _load_thread_only_display():
    return _load_module(PLUGIN_DIR.parent / "thread-only-display" / "__init__.py", "thread_only_display_under_test")


def _unload(module):
    for callback in module.ctx.unload:
        callback()


@pytest.fixture
def plugin(monkeypatch):
    import gateway.run  # noqa: F401  (a gateway process: the seams install at register)
    import plugins.platforms.discord.adapter  # noqa: F401
    module = _load_plugin()
    monkeypatch.setattr(module, "EDIT_INTERVAL", 0.05)
    module.register(module.ctx)
    HOOKS.clear()
    HOOKS.update(module.ctx.hooks)
    yield module
    HOOKS.clear()
    _unload(module)


# ── Discord fakes ────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, channel, message_id, kwargs):
        self.channel = channel
        self.id = message_id
        self.sent = dict(kwargs, labels=_labels(kwargs.get("view")))  # the view object is mutated later
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(dict(kwargs, labels=_labels(kwargs.get("view"))))
        return self

    @property
    def latest(self) -> dict:
        state = dict(self.sent)
        for edit in self.edits:
            state.update(edit)
        return state


class FakeChannel:
    def __init__(self, channel_id, name):
        self.id = int(channel_id)
        self.name = name
        self.messages: list[FakeMessage] = []

    async def send(self, **kwargs):
        message = FakeMessage(self, 9000 + len(self.messages), kwargs)
        self.messages.append(message)
        return message


def _adapter_cls():
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    class CaptureDiscordAdapter(DiscordAdapter):
        def __init__(self, platform=None):
            super().__init__(PlatformConfig(enabled=True, token="***"))
            self.sent: list[dict] = []
            self.edits: list[dict] = []
            self.channels: dict[str, FakeChannel] = {}
            self.slash: list[tuple] = []
            self._allowed_user_ids = {ALLOWED}

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
            self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
            return SendResult(success=True, message_id=f"text-{len(self.sent)}")

        async def edit_message(self, chat_id, message_id, content, **_kwargs) -> SendResult:
            self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None) -> None:
            return None

        async def stop_typing(self, chat_id) -> None:
            return None

        async def _resolve_channel(self, channel_id):
            key = str(channel_id)
            if key not in self.channels:
                self.channels[key] = FakeChannel(key, "time-check thread" if key == THREAD else f"ch-{key}")
            return self.channels[key]

        def _component_live_auth(self, interaction):
            return None

        async def _run_simple_slash(self, interaction, command_text, followup_msg=None):
            self.slash.append((interaction, command_text))

    return CaptureDiscordAdapter


def _cards(adapter) -> list[FakeMessage]:
    return [message for channel in adapter.channels.values() for message in channel.messages]


def _source(*, chat_id=THREAD, thread_id=THREAD, parent=CONSOLE, chat_type="thread", platform=None,
            user_id=REQUESTER):
    from gateway.config import Platform
    from gateway.session import SessionSource
    return SessionSource(platform=platform or Platform.DISCORD, chat_id=chat_id, chat_type=chat_type,
                         thread_id=thread_id, parent_chat_id=parent, user_id=user_id)


THREAD_SOURCE = {}
PARENT_SOURCE = {"chat_id": CONSOLE, "thread_id": None, "parent": None, "chat_type": "group"}
OTHER_THREAD_SOURCE = {"chat_id": OTHER_THREAD, "thread_id": OTHER_THREAD, "parent": OTHER_PARENT}


# ── matcher ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("configured", "source_kw", "expected"),
    [
        ([CONSOLE], THREAD_SOURCE, True),
        ([int(CONSOLE)], THREAD_SOURCE, True),
        (f"{CONSOLE}, 42", THREAD_SOURCE, True),
        (int(CONSOLE), THREAD_SOURCE, True),
        ([CONSOLE], PARENT_SOURCE, False),  # the parent channel itself
        ([CONSOLE], OTHER_THREAD_SOURCE, False),  # a thread under another channel
        ([CONSOLE], {"thread_id": None, "chat_type": "group"}, False),
        ([], THREAD_SOURCE, False),
        (None, THREAD_SOURCE, False),
    ],
)
def test_is_status_card_source_channel_opt_in(configured, source_kw, expected):
    match = _load_plugin().is_status_card_source
    assert match({"display": {"status_card_channels": configured}}, _source(**source_kw)) is expected


def test_only_discord_threads_match():
    from gateway.config import Platform
    match = _load_plugin().is_status_card_source
    config = {"display": {"status_card_channels": [CONSOLE]}}
    assert match(config, _source(platform=Platform.SLACK)) is False
    assert match({}, _source()) is False
    assert match({"display": "bad"}, _source()) is False
    assert match(None, _source()) is False


@pytest.mark.parametrize("source_kw", [THREAD_SOURCE, OTHER_THREAD_SOURCE, PARENT_SOURCE,
                                      {"chat_id": "444", "thread_id": None, "parent": None, "chat_type": "dm"}])
@pytest.mark.parametrize("enabled", [True, False, None, "true"])
def test_global_selector_requires_boolean_and_covers_discord_destinations(source_kw, enabled):
    match = _load_plugin().is_status_card_source
    assert match({"display": {"status_cards": enabled}}, _source(**source_kw)) is (enabled is True)


def test_global_selector_preserves_platform_boundary_and_channel_opt_in():
    from gateway.config import Platform
    match = _load_plugin().is_status_card_source
    config = {"display": {"status_cards": True}}
    assert match(config, _source(platform=Platform.SLACK)) is False
    assert match(config, _source(chat_id="", thread_id=None, parent=None)) is False
    scoped = {"display": {"status_cards": False, "status_card_channels": [CONSOLE]}}
    assert match(scoped, _source()) is True
    assert match(scoped, _source(**OTHER_THREAD_SOURCE)) is False
    assert match(scoped, _source(**PARENT_SOURCE)) is False


@pytest.mark.parametrize("quiet_field", ["scheduled_heartbeat", "mute_notification_reply"])
def test_global_cards_keep_quiet_turns_quiet(plugin, monkeypatch, quiet_field):
    opened = []
    monkeypatch.setattr(plugin, "_open_card", lambda *_args: opened.append(True))
    ctx = SimpleNamespace(user_config={"display": {"status_cards": True}},
                          scheduled_heartbeat=False, mute_notification_reply=False)
    setattr(ctx, quiet_field, True)
    runner = SimpleNamespace(_delivery_adapter_for=lambda _source: _adapter_cls()())
    wrapped = plugin._wrap_bind(type(runner), lambda *_args: "core-result")
    assert wrapped(runner, ctx, None, _source(**PARENT_SOURCE), None, True) == "core-result"
    assert opened == []


# ── per-turn display settings ────────────────────────────────────────


def _runner(monkeypatch, tmp_path, display, adapter=None):
    import hermes_yaml as yaml
    from tests.gateway.test_run_progress_topics import _make_runner

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"display": display}), encoding="utf-8")
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    return _make_runner(adapter or _adapter_cls()())


def _display(monkeypatch, tmp_path, source, display=DISPLAY, adapter=None):
    return _runner(monkeypatch, tmp_path, display, adapter)._run_agent_display_settings(source)


def _assert_normal_display(disp):
    assert disp._native_slack_task_cards is False
    assert disp.tool_progress_enabled is True
    assert disp.interim_assistant_messages_enabled is True
    assert disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True) == "raw"


def test_core_shows_normal_display_in_the_thread(monkeypatch, tmp_path):
    """Without the plugin a listed thread gets text progress, interim messages and the heartbeat."""
    _assert_normal_display(_display(monkeypatch, tmp_path, _source()))


def test_listed_thread_switches_to_the_card_lane(plugin, monkeypatch, tmp_path):
    disp = _display(monkeypatch, tmp_path, _source())

    assert disp._native_slack_task_cards is True
    assert disp.needs_progress_queue is True
    assert disp.tool_progress_enabled is False
    assert disp.interim_assistant_messages_enabled is False
    assert disp._thinking_enabled is False
    assert disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True) == "off"
    # Other surfaces still resolve through the core.
    assert disp._display_surface_mode("interim_assistant_messages", default=True) == "raw"


@pytest.mark.parametrize("source_kw", [PARENT_SOURCE, OTHER_THREAD_SOURCE])
def test_parent_and_other_channels_keep_normal_display(plugin, monkeypatch, tmp_path, source_kw):
    _assert_normal_display(_display(monkeypatch, tmp_path, _source(**source_kw)))


@pytest.mark.parametrize("global_cards", [False, True])
def test_explicit_tool_progress_off_keeps_cards_off(plugin, monkeypatch, tmp_path, global_cards):
    disp = _display(monkeypatch, tmp_path, _source(),
                    {**DISPLAY, "status_cards": global_cards, "tool_progress": "off"})
    assert disp._native_slack_task_cards is False
    assert disp.tool_progress_enabled is False
    assert disp.interim_assistant_messages_enabled is True


def test_adapter_without_card_support_keeps_normal_display(plugin, monkeypatch, tmp_path):
    from gateway.config import Platform
    from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter
    disp = _display(monkeypatch, tmp_path, _source(), adapter=ProgressCaptureAdapter(platform=Platform.DISCORD))
    _assert_normal_display(disp)


# ── heartbeat ────────────────────────────────────────────────────────


async def _heartbeats(monkeypatch, tmp_path, source) -> list[dict]:
    from gateway.turn_context import TurnContext
    adapter = _adapter_cls()()
    runner = _runner(monkeypatch, tmp_path, DISPLAY, adapter)
    disp = runner._run_agent_display_settings(source)
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0.02")
    monkeypatch.setattr(runner, "_should_emit_long_running_notification", lambda *_a: True, raising=False)
    monkeypatch.setattr(runner, "_agent_activity_summary", lambda *_a: None, raising=False)
    turn_ctx = TurnContext(source=source, session_key="k", agent_holder=[object()])
    task = asyncio.create_task(runner._run_agent_notify_long_running(disp, turn_ctx, [None]))
    await asyncio.sleep(0.15)
    task.cancel()  # a disabled heartbeat has already returned
    with suppress(asyncio.CancelledError):
        await task
    return adapter.sent


@pytest.mark.asyncio
async def test_heartbeat_is_suppressed_only_in_listed_threads(plugin, monkeypatch, tmp_path):
    assert await _heartbeats(monkeypatch, tmp_path, _source()) == []
    assert await _heartbeats(monkeypatch, tmp_path, _source(**PARENT_SOURCE))


# ── turns end to end through GatewayRunner._run_agent ───────────────


def _fire(name, **kwargs):
    for callback in HOOKS.get(name, []):
        callback(**kwargs)


def _noop(*_args, **_kwargs):
    return None


class CardAgent:
    """One turn: interim text, reasoning + usage, two tools, final usage, then on_session_end."""

    interim = "Let me look around first."
    reasoning = "Need to list the files before answering."
    calls = (("call-1", "terminal", {"command": "ls -la"}, '{"output": "ok"}', "ok", None),
             ("call-2", "read_file", {"path": "README.md"}, '{"error": "missing"}', "error", "missing"))
    outcome = {"completed": True, "failed": False, "interrupted": False}
    session_key = None

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.interim_assistant_callback = None
        self.tool_start_callback = None
        self.tool_complete_callback = None
        self.tools = []

    def _turn_body(self):
        progress = self.tool_progress_callback or _noop
        start = self.tool_start_callback or _noop
        complete = self.tool_complete_callback or _noop
        if self.interim_assistant_callback:
            self.interim_assistant_callback(self.interim, already_streamed=False)
        _fire("on_interim_message", text=self.interim, already_streamed=False, session_id="s1", turn_id="t1")
        _fire("post_api_request", session_id="s1", turn_id="t1", platform="discord",
              usage={"input_tokens": 1000, "prompt_tokens": 1200, "output_tokens": 80},
              assistant_message=SimpleNamespace(content="", reasoning=self.reasoning, tool_calls=[1]))
        for call_id, name, args, result, status, error in self.calls:
            progress("tool.started", name, next(iter(args.values())), args)
            start(call_id, name, args)
            time.sleep(0.12)
            complete(call_id, name, args, result)
            _fire("post_tool_call", tool_name=name, args=args, result=result, task_id="", session_id="s1",
                  tool_call_id=call_id, turn_id="t1", api_request_id="", duration_ms=120, status=status,
                  error_type="tool_error" if error else None, error_message=error, middleware_trace=[])
        _fire("post_api_request", session_id="s1", turn_id="t1", platform="discord",
              usage={"input_tokens": 1300, "prompt_tokens": 1500, "output_tokens": 40},
              assistant_message=SimpleNamespace(content="done"))

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        self._turn_body()
        _fire("on_session_end", session_id="s1", task_id="", turn_id="t1", turn_exit_reason="text_response(stop)",
              model="m", platform="discord", **self.outcome)
        time.sleep(0.1)
        return {"final_response": "done", "messages": [], "api_calls": 2}


class FailingAgent(CardAgent):
    outcome = {"completed": False, "failed": True, "interrupted": False}


class InterruptedAgent(CardAgent):
    outcome = {"completed": False, "failed": False, "interrupted": True}

    def _turn_body(self):
        super()._turn_body()
        _fire("agent_loop_stopped", session_key=self.session_key, platform="discord", reason="stop",
              invalidation_reason=None)


class BurstAgent(CardAgent):
    """Twelve tool calls 20 ms apart: the card must debounce, not edit per event."""

    calls = tuple((f"call-{i}", "terminal", {"command": f"echo {i}"}, '{"output": "ok"}', "ok", None)
                  for i in range(12))

    def _turn_body(self):
        start = self.tool_start_callback or _noop
        complete = self.tool_complete_callback or _noop
        for call_id, name, args, result, *_ in self.calls:
            start(call_id, name, args)
            complete(call_id, name, args, result)
            time.sleep(0.02)
        time.sleep(0.3)


class ChildEventsAgent(CardAgent):
    """A delegate_task child running in this turn's context: its events are not the parent's."""

    def _turn_body(self):
        from agent.delegation_context import delegated_child_context
        super()._turn_body()
        with delegated_child_context("child-session"):
            _fire("on_interim_message", text="child chatter", already_streamed=False, session_id="c", turn_id="c")
            _fire("post_api_request", session_id="c", turn_id="c", platform="discord",
                  usage={"prompt_tokens": 99999, "output_tokens": 99999},
                  assistant_message=SimpleNamespace(content="", reasoning="child thinking"))
            _fire("on_session_end", session_id="c", task_id="", turn_id="c", completed=False, failed=True,
                  interrupted=False, turn_exit_reason="x", model="m", platform="discord")


class LongLogAgent(CardAgent):
    interim = "long interim " + "x" * 5000


async def _turn(monkeypatch, tmp_path, agent_cls, source_kw=THREAD_SOURCE, display=DISPLAY, holder=None):
    import hermes_yaml as yaml
    from gateway.config import StreamingConfig
    from gateway.session_context import clear_session_vars, set_session_vars
    from tests.gateway.test_run_progress_topics import _make_runner

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"display": display, "streaming": {"enabled": False}}), encoding="utf-8")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    adapter = _adapter_cls()()
    if holder is not None:
        holder["adapter"] = adapter
    runner = _make_runner(adapter)
    runner.config.streaming = StreamingConfig.from_dict({"enabled": False})
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    source = _source(**source_kw)
    session_key = f"agent:main:discord:{source.chat_type}:{source.chat_id}"
    monkeypatch.setattr(agent_cls, "session_key", session_key)
    tokens = set_session_vars(platform="discord", chat_id=source.chat_id, thread_id=source.thread_id or "",
                              session_key=session_key, user_id=REQUESTER)
    try:
        result = await runner._run_agent(message="hello", context_prompt="", history=[], source=source,
                                         session_id="s1", session_key=session_key)
    finally:
        clear_session_vars(tokens)
    return adapter, result


async def _eventually(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _labels(view) -> list[str]:
    return [getattr(item, "label", None) for item in view.children] if view is not None else []


async def _final_card(adapter, title_prefix):
    assert await _eventually(lambda: _cards(adapter) and str(
        _cards(adapter)[0].latest["embed"].title or "").startswith(title_prefix)), (
        [m.latest["embed"].title for m in _cards(adapter)])
    await asyncio.sleep(0.15)  # nothing else may follow the final edit
    return _cards(adapter)[0]


@pytest.mark.asyncio
async def test_core_turn_in_thread_sends_text_progress_and_interim(monkeypatch, tmp_path):
    """Baseline without the plugin: what the card replaces is really sent."""
    adapter, result = await _turn(monkeypatch, tmp_path, CardAgent)

    texts = [call["content"] for call in adapter.sent] + [edit["content"] for edit in adapter.edits]
    assert result["final_response"] == "done"
    assert CardAgent.interim in texts
    assert any("ls -la" in text for text in texts)
    assert _cards(adapter) == []


@pytest.mark.asyncio
async def test_listed_thread_turn_shows_one_card_and_no_progress_messages(plugin, monkeypatch, tmp_path):
    adapter, result = await _turn(monkeypatch, tmp_path, CardAgent)
    card = await _final_card(adapter, "✅")

    assert result["final_response"] == "done"
    assert adapter.sent == [] and adapter.edits == []  # no tool progress, no interim, no heartbeat
    assert len(_cards(adapter)) == 1
    assert list(adapter.channels) == [THREAD]
    first = card.sent["embed"]
    assert first.title.startswith("💭 推論中") and "time-check thread" in first.title
    assert card.sent["labels"] == ["詳細", "中断"]
    final = card.latest
    assert final["embed"].title == "✅ 完了 — time-check thread"
    description = final["embed"].description
    assert description.startswith("✅ 完了 · ") and "ツール 2" in description
    assert final["labels"] == ["詳細"]


@pytest.mark.asyncio
async def test_card_shows_reasoning_tool_and_tokens_while_running(plugin, monkeypatch, tmp_path):
    adapter, _ = await _turn(monkeypatch, tmp_path, CardAgent)
    card = await _final_card(adapter, "✅")
    running = [edit["embed"].description for edit in [card.sent, *card.edits]
               if not edit["embed"].title.startswith("✅")]

    assert any("最終イベント <t:" in text and "開始 <t:" in text for text in running)
    assert any(CardAgent.reasoning in text or CardAgent.interim in text for text in running)
    assert any("terminal" in text and "ls -la" in text for text in running)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kw", [PARENT_SOURCE, OTHER_THREAD_SOURCE])
async def test_parent_and_other_threads_get_no_card(plugin, monkeypatch, tmp_path, source_kw):
    adapter, _ = await _turn(monkeypatch, tmp_path, CardAgent, source_kw)
    await asyncio.sleep(0.2)

    texts = [call["content"] for call in adapter.sent]
    assert _cards(adapter) == []
    assert CardAgent.interim in texts


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kw", [THREAD_SOURCE, OTHER_THREAD_SOURCE, PARENT_SOURCE,
                                      {"chat_id": "444", "thread_id": None, "parent": None, "chat_type": "dm"}])
async def test_global_turn_renders_one_card_at_its_destination(plugin, monkeypatch, tmp_path, source_kw):
    adapter, result = await _turn(monkeypatch, tmp_path, CardAgent, source_kw,
                                  display={"tool_progress": "all", "status_cards": True})
    card = await _final_card(adapter, "✅")
    assert result["final_response"] == "done"
    assert adapter.sent == [] and adapter.edits == []
    assert len(_cards(adapter)) == 1
    source = _source(**source_kw)
    assert str(card.channel.id) == str(source.thread_id or source.chat_id)
    assert card.sent["labels"] == ["詳細", "中断"]
    assert card.latest["labels"] == ["詳細"]
    assert "ツール 2" in card.latest["embed"].description


@pytest.mark.asyncio
async def test_failed_turn_ends_with_failed_card(plugin, monkeypatch, tmp_path):
    adapter, _ = await _turn(monkeypatch, tmp_path, FailingAgent)
    card = await _final_card(adapter, "⚠️")
    assert card.latest["embed"].title.startswith("⚠️ 失敗")
    assert card.latest["embed"].description.startswith("⚠️ 失敗 · ")
    assert card.latest["labels"] == ["詳細"]


@pytest.mark.asyncio
async def test_interrupted_turn_ends_with_interrupted_card(plugin, monkeypatch, tmp_path):
    adapter, _ = await _turn(monkeypatch, tmp_path, InterruptedAgent)
    card = await _final_card(adapter, "⏹")
    assert card.latest["embed"].title.startswith("⏹ 中断")
    assert card.latest["labels"] == ["詳細"]


@pytest.mark.asyncio
async def test_card_edits_are_debounced(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "EDIT_INTERVAL", 0.25)
    adapter, _ = await _turn(monkeypatch, tmp_path, BurstAgent)
    card = await _final_card(adapter, "✅")

    assert "ツール 12" in card.latest["embed"].description
    # ~0.55 s of events at one write per 0.25 s: the send, two or three edits, the final edit.
    assert len(card.edits) <= 4, [edit["embed"].description for edit in card.edits]


@pytest.mark.asyncio
async def test_delegate_child_events_do_not_reach_the_parent_card(plugin, monkeypatch, tmp_path):
    adapter, _ = await _turn(monkeypatch, tmp_path, ChildEventsAgent)
    card = await _final_card(adapter, "✅")
    shown = [edit["embed"].description for edit in [card.sent, *card.edits]]
    assert not any("child" in text for text in shown)


# ── buttons ──────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    def is_done(self):
        return bool(self.messages)


def _interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=int(user_id), roles=[]), response=FakeResponse(),
                           channel_id=int(THREAD), guild=SimpleNamespace(id=1))


def _button(view, label):
    return next(item for item in view.children if getattr(item, "label", None) == label)


class SlowAgent(CardAgent):
    """Keeps the turn running so the stop button is live."""

    def _turn_body(self):
        super()._turn_body()
        time.sleep(0.6)


@pytest.mark.asyncio
async def test_stop_button_is_refused_to_strangers_and_runs_stop_for_requester(plugin, monkeypatch, tmp_path):
    holder = {}
    turn = asyncio.create_task(_turn(monkeypatch, tmp_path, SlowAgent, holder=holder))
    assert await _eventually(lambda: "adapter" in holder and _cards(holder["adapter"]))
    adapter = holder["adapter"]
    view = _cards(adapter)[0].sent["view"]
    stop = _button(view, "中断")

    stranger = _interaction(STRANGER)
    await stop.callback(stranger)
    assert adapter.slash == []
    assert stranger.response.messages and stranger.response.messages[0]["ephemeral"] is True

    requester = _interaction(REQUESTER)
    await stop.callback(requester)
    assert [command for _i, command in adapter.slash] == ["/stop"]
    assert adapter.slash[0][0] is requester

    allowed = _interaction(ALLOWED)
    await stop.callback(allowed)
    assert [command for _i, command in adapter.slash] == ["/stop", "/stop"]
    await turn


@pytest.mark.asyncio
async def test_details_button_returns_the_full_log(plugin, monkeypatch, tmp_path):
    adapter, _ = await _turn(monkeypatch, tmp_path, CardAgent)
    card = await _final_card(adapter, "✅")
    interaction = _interaction(STRANGER)

    await _button(card.latest["view"], "詳細").callback(interaction)

    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True
    text = reply["embed"].description
    for expected in ("terminal", "ls -la", "read_file", "README.md", "missing", "120ms",
                     CardAgent.interim, CardAgent.reasoning, "✅ 完了"):
        assert expected in text, expected


@pytest.mark.asyncio
async def test_long_details_log_is_attached_as_a_file(plugin, monkeypatch, tmp_path):
    adapter, _ = await _turn(monkeypatch, tmp_path, LongLogAgent)
    card = await _final_card(adapter, "✅")
    interaction = _interaction(REQUESTER)

    await _button(card.latest["view"], "詳細").callback(interaction)

    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True
    data = reply["file"].fp.read().decode("utf-8")
    assert LongLogAgent.interim in data and "read_file" in data


# ── install / private-API guards ─────────────────────────────────────


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_registers_on_this_hermes_without_a_warning(caplog):
    import gateway.run  # noqa: F401
    from plugins.platforms.discord.adapter import DiscordAdapter
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
        module.install()
    try:
        assert _warnings(caplog) == []
        assert callable(getattr(DiscordAdapter, "send_native_task_card_progress", None))
        assert callable(getattr(DiscordAdapter, "stop_native_task_card_progress", None))
    finally:
        _unload(module)
    assert not hasattr(DiscordAdapter, "send_native_task_card_progress")
    assert "_run_agent_display_settings" not in gateway.run.GatewayRunner.__dict__


def test_core_rail_is_still_slack_only_and_method_driven():
    """The plugin relies on the rail calling adapter methods once the display flag is set."""
    import inspect
    from gateway.run_turn_progress import GatewayTurnProgressMixin
    rail = inspect.getsource(GatewayTurnProgressMixin.send_progress_messages)
    assert 'hasattr(adapter, "send_native_task_card_progress")' in rail
    stop = inspect.getsource(GatewayTurnProgressMixin._send_native_task_card_progress)
    assert "stop_native_task_card_progress" in stop


@pytest.mark.asyncio
async def test_install_is_deferred_until_gateway_loaded(monkeypatch):
    import gateway.run as gateway_run
    monkeypatch.delitem(sys.modules, "gateway.run")
    module = _load_plugin()
    module.register(module.ctx)
    assert "_run_agent_display_settings" not in gateway_run.GatewayRunner.__dict__
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    try:
        await module.ctx.hooks["pre_gateway_dispatch"][0](event=None, gateway=None, session_store=None)
        assert "_run_agent_display_settings" in gateway_run.GatewayRunner.__dict__
    finally:
        _unload(module)


def test_missing_adapter_seam_warns_once_and_keeps_normal_display(monkeypatch, tmp_path, caplog):
    import gateway.run  # noqa: F401
    from plugins.platforms.discord.adapter import DiscordAdapter
    monkeypatch.delattr(DiscordAdapter, "_resolve_channel")
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
        module.install()
    try:
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "_resolve_channel" in warnings[0] and "per-tool progress" in warnings[0]
        assert not hasattr(DiscordAdapter, "send_native_task_card_progress")
        _assert_normal_display(_display(monkeypatch, tmp_path, _source()))
    finally:
        _unload(module)


def test_resigned_turn_seam_warns_once_and_keeps_normal_display(monkeypatch, tmp_path, caplog):
    from gateway.run_turn import GatewayTurnMixin
    monkeypatch.setattr(GatewayTurnMixin, "_run_agent_bind_turn_wiring",
                        lambda self, turn_ctx, source: None)
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
        module.install()
    try:
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "_run_agent_bind_turn_wiring" in warnings[0] and "signature changed" in warnings[0]
        _assert_normal_display(_display(monkeypatch, tmp_path, _source()))
    finally:
        _unload(module)


def test_display_dataclass_change_warns_and_keeps_core(monkeypatch, caplog):
    import dataclasses
    import gateway.run as gateway_run

    @dataclasses.dataclass
    class Renamed:
        progress_mode: object = None

    monkeypatch.setattr(gateway_run.GatewayRunner, "_RunAgentDisplay", Renamed)
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    try:
        warnings = _warnings(caplog)
        assert len(warnings) == 1 and "_RunAgentDisplay lacks fields" in warnings[0]
        assert "_run_agent_display_settings" not in gateway_run.GatewayRunner.__dict__
    finally:
        _unload(module)


def test_coexists_with_thread_only_display(monkeypatch, tmp_path):
    import gateway.display_config as display_config
    import gateway.run  # noqa: F401
    # The pinned fork tree still carries thread-only-display's core version: neutralize it (as its suite does).
    monkeypatch.setattr(display_config, "is_thread_only_display_channel", lambda *_a, **_k: False, raising=False)
    display = {**DISPLAY, "thread_only_channels": [OTHER_PARENT]}
    other_parent = {"chat_id": OTHER_PARENT, "thread_id": None, "parent": None, "chat_type": "group"}

    def check(*, cards, thread_only):
        disp = _display(monkeypatch, tmp_path, _source(), display)
        assert disp._native_slack_task_cards is cards
        disp = _display(monkeypatch, tmp_path, _source(**other_parent), display)
        assert (disp.tool_progress_enabled is False) is thread_only

    tod, card = _load_thread_only_display(), _load_plugin()
    tod.register(tod.ctx)
    card.register(card.ctx)
    asyncio.run(tod.ctx.hooks["pre_gateway_dispatch"][0]())
    asyncio.run(card.ctx.hooks["pre_gateway_dispatch"][0]())
    check(cards=True, thread_only=True)
    _unload(tod)
    check(cards=True, thread_only=False)
    tod2 = _load_thread_only_display()
    tod2.register(tod2.ctx)
    check(cards=True, thread_only=True)
    _unload(card)
    check(cards=False, thread_only=True)
    _unload(tod2)
    check(cards=False, thread_only=False)


@pytest.fixture
def delegated_family(plugin, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    import tools.delegate_tool_config as dtc
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dtc, "_load_config", dict)
    monkeypatch.setattr(dt, "_build_child_system_prompt", lambda *_a, **_kw: "CHILD PROMPT")
    parent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter",
                     api_mode="chat_completions", model="anthropic/claude-sonnet-4.6", quiet_mode=True,
                     skip_memory=True, save_trajectories=False, enabled_toolsets=["file"], session_id="parent")
    children = []

    def build(goal, index=0):
        child = dt._build_child_agent(task_index=index, goal=goal, context=None, toolsets=["file"], model=None,
                                     max_iterations=4, task_count=1, parent_agent=parent)
        children.append(child)
        return child

    yield parent, build
    for child in children:
        child.close()
    parent.close()


def _open_parent_card(plugin, adapter):
    from gateway.session_context import set_session_vars
    tokens = set_session_vars(platform="discord", chat_id=THREAD, thread_id=THREAD, session_key="parent-route",
                              user_id=REQUESTER)
    plugin._open_card(adapter, SimpleNamespace(session_key="parent-route", _progress_metadata={}), _source())
    return plugin._cards["parent-route"], tokens


def _script_child(child, text, gate=None, *, fail=False):
    def converse(**_kwargs):
        start = child.tool_start_callback
        if start:
            start("same-call-id", "terminal", {"command": text})
        _fire("on_interim_message", text=text, session_id=child.session_id)
        _fire("post_api_request", session_id=child.session_id, usage={"prompt_tokens": 123, "output_tokens": 7})
        if gate is not None:
            gate.wait(5)
        _fire("post_tool_call", session_id=child.session_id, tool_call_id="same-call-id", tool_name="terminal",
              duration_ms=25, status="ok")
        return {"final_response": text, "messages": [], "api_calls": 1, "completed": not fail, "failed": fail,
                "interrupted": child._interrupt_requested}
    child.run_conversation = converse


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_child_cards_survive_parent_and_isolate_parallel_events(plugin, delegated_family, background):
    from gateway.session_context import clear_session_vars
    from tools import delegate_tool as dt
    from tools.delegate_tool_child_run import _detach_child
    parent, build = delegated_family
    adapter = _adapter_cls()()
    parent_card, tokens = _open_parent_card(plugin, adapter)
    try:
        a, b = build("task A"), build("task B", 1)
        _script_child(a, "work A")
        _script_child(b, "work B", fail=True)
        if background:
            _detach_child(parent, a)
            _detach_child(parent, b)
            parent_card.on_outcome(completed=True, failed=False, interrupted=False, reason="done")
            parent_card.finish()
            plugin._cards.clear()  # the gateway retires the parent's rail before children finish
        results = await asyncio.gather(
            asyncio.to_thread(dt._run_single_child, 0, "task A", a, parent),
            asyncio.to_thread(dt._run_single_child, 1, "task B", b, parent))
        assert [r["status"] for r in results] == ["completed", "failed"]
        assert await _eventually(lambda: len(_cards(adapter)) == 3)
        assert await _eventually(lambda: all(m.latest["labels"] == ["詳細"] for m in _cards(adapter)[1:]))
        cards = {m.latest["embed"].title: m for m in _cards(adapter)[1:]}
        ac = next(m for title, m in cards.items() if "task A" in title)
        bc = next(m for title, m in cards.items() if "task B" in title)
        assert ac.latest["embed"].title.startswith("✅")
        assert bc.latest["embed"].title.startswith("⚠️")
        for message, own, other in ((ac, "work A", "work B"), (bc, "work B", "work A")):
            card = message.latest["view"].card
            assert own in card.details_text() and other not in card.details_text()
            assert "ツール 1" in message.latest["embed"].description
        assert parent_card.tokens_in == 0 and parent_card.tools == {}
    finally:
        clear_session_vars(tokens)
        parent_card.finish()


@pytest.mark.asyncio
async def test_child_stop_targets_only_authorized_child(plugin, delegated_family):
    import threading
    from gateway.session_context import clear_session_vars
    from tools import delegate_tool as dt
    parent, build = delegated_family
    adapter = _adapter_cls()()
    parent_card, tokens = _open_parent_card(plugin, adapter)
    gate = threading.Event()
    child = build("stop this child")
    _script_child(child, "waiting", gate)
    task = asyncio.create_task(asyncio.to_thread(dt._run_single_child, 0, "stop this child", child, parent))
    try:
        assert await _eventually(lambda: len(_cards(adapter)) == 2)
        card = _cards(adapter)[1].latest["view"].card
        stranger = _interaction(STRANGER)
        await card.request_stop(stranger)
        assert not child._interrupt_requested and stranger.response.messages[0]["ephemeral"]
        await card.request_stop(_interaction(REQUESTER))
        assert child._interrupt_requested and not parent._interrupt_requested and adapter.slash == []
        gate.set()
        assert (await task)["status"] == "interrupted"
        assert await _eventually(lambda: _cards(adapter)[1].latest["embed"].title.startswith("⏹"))
    finally:
        gate.set()
        await task
        clear_session_vars(tokens)
        parent_card.finish()


@pytest.mark.asyncio
async def test_codex_child_tool_completion_without_post_tool_hook(plugin, delegated_family):
    from agent.codex_runtime import make_codex_app_server_event_bridge
    from gateway.session_context import clear_session_vars
    from tools import delegate_tool as dt
    parent, build = delegated_family
    adapter = _adapter_cls()()
    parent_card, tokens = _open_parent_card(plugin, adapter)
    child = build("Codex child")

    def converse(**_kwargs):
        bridge = make_codex_app_server_event_bridge(child)
        item = {"type": "commandExecution", "id": "cmd-1", "command": "false", "status": "inProgress"}
        bridge({"method": "item/started", "params": {"item": item}})
        bridge({"method": "item/completed", "params": {"item": {
            **item, "status": "completed", "exitCode": 1, "aggregatedOutput": "failed", "durationMs": 12}}})
        return {"final_response": "reported failure", "messages": [], "api_calls": 1, "completed": True}

    child.run_conversation = converse
    try:
        await asyncio.to_thread(dt._run_single_child, 0, "Codex child", child, parent)
        assert await _eventually(lambda: len(_cards(adapter)) == 2)
        card = _cards(adapter)[1].latest["view"].card
        tool, = card.tools.values()
        assert tool["ended"] is not None and tool["status"] == "error"
    finally:
        clear_session_vars(tokens)
        parent_card.finish()
