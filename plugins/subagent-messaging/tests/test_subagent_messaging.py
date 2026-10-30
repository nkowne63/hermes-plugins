"""Tests for the subagent-messaging plugin.

The plugin is installed into a scratch profile home and loaded by Hermes' real plugin discovery (a real
``PluginContext``, so ``inject_message`` / ``get_config`` run the core code). Agents are REAL ``AIAgent``s: a
top-level parent plus ``delegate_task`` children built by ``tools.delegate_tool._build_child_agent`` and
registered in the live subagent registry exactly as ``_run_single_child`` does. Tool calls go through
``model_tools.handle_function_call`` (hooks, middleware, registry dispatch) with the child's ``task_id``.
The gateway is represented by the process-wide plugin message host it publishes
(``hermes_cli.plugins.publish_gateway_message_host``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"
PLUGIN_KEY = "subagent-messaging"
TOOLSET = "subagent-messaging"
TOOLS = {"subagent_peers", "subagent_send", "subagent_wait"}
ROUTE = "agent:main:discord:thread:4242"
_AGENT_KW = dict(
    api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter", api_mode="chat_completions",
    model="anthropic/claude-sonnet-4.6", platform="cli", quiet_mode=True, skip_memory=True,
    save_trajectories=False,
)


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """``hermes(settings=None, injection=True)`` writes the profile config and loads the plugin; returns its module."""
    if not HERMES_TREE.is_dir():
        pytest.skip(f"Hermes tree not found at {HERMES_TREE}")
    monkeypatch.syspath_prepend(str(HERMES_TREE))
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / PLUGIN_KEY).symlink_to(PLUGIN_DIR, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", f"/pytest-{tmp_path.name}")
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    def load(settings: dict | None = None, *, injection: bool = True):
        entry = {"allow_gateway_injection": injection, **({"settings": settings} if settings else {})}
        config = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: entry}}}
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")  # JSON is valid YAML
        from hermes_cli.plugins import _ensure_plugins_discovered
        manager = _ensure_plugins_discovered(force=True)
        loaded = manager._plugins.get(PLUGIN_KEY)
        assert loaded is not None and loaded.enabled and not loaded.error, f"plugin not loaded: {loaded!r}"
        return loaded.module

    yield load
    from hermes_cli.plugins import _reset_plugin_managers_for_tests
    _reset_plugin_managers_for_tests()


@pytest.fixture
def gateway():
    """The live gateway's plugin message host: records every injected turn and accepts it."""
    from hermes_cli.plugins import clear_published_gateway_message_host, publish_gateway_message_host
    owner, turns = object(), []

    def inject(**kwargs):
        turns.append(kwargs)
        return True

    publish_gateway_message_host(owner, inject)
    yield turns
    clear_published_gateway_message_host(owner)


class Family:
    """Builds real parents and delegate_task children; tears everything down."""

    def __init__(self):
        self._agents, self._registered = [], []

    def parent(self, session_id: str, route: str | None = ROUTE):
        from run_agent import AIAgent
        agent = AIAgent(**_AGENT_KW, enabled_toolsets=["file", TOOLSET], session_id=session_id)
        agent._gateway_session_key = route  # what the gateway stamps on the agent it runs for a chat
        self._agents.append(agent)
        return agent

    def child(self, parent, goal: str, index: int = 0):
        from tools import delegate_tool as dt
        from tools.delegate_tool_child_run import _register_child
        child = dt._build_child_agent(task_index=index, goal=goal, context=None, toolsets=None, model=None,
                                      max_iterations=4, task_count=1, parent_agent=parent)
        self._agents.append(child)
        _register_child(child, parent, goal, owner_session_id=None, owner_transport=None, owner_session_record=None)
        self._registered.append(child)
        return child

    def close(self):
        from tools.delegate_tool_registry import _unregister_subagent
        for child in self._registered:
            _unregister_subagent(child._subagent_id, agent=child)
        for agent in reversed(self._agents):
            agent.close()


@pytest.fixture
def family(monkeypatch):
    from tools import delegate_tool as dt
    import tools.delegate_tool_config as dtc
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dtc, "_load_config", dict)
    monkeypatch.setattr(dt, "_build_child_system_prompt", lambda *_a, **_kw: "CHILD PROMPT")
    built = Family()
    yield built
    built.close()


def call(agent, tool: str, **args) -> dict:
    """One model tool call by *agent*, run the way its turn runs it (a child's turn is inside
    ``delegated_child_context``, see ``_ChildRun.await_child``)."""
    from agent.delegation_context import delegated_child_context
    from model_tools import handle_function_call
    task_id = getattr(agent, "_subagent_id", None)
    with delegated_child_context(agent.session_id) if task_id else contextlib.nullcontext():
        raw = handle_function_call(tool, args, task_id=task_id, session_id=agent.session_id)
    return json.loads(raw)


def pending(agent) -> str:
    """The steer text queued for *agent* (what its next tool boundary delivers), drained."""
    return agent._drain_pending_steer() or ""


def tool_names(agent) -> set:
    return {t["function"]["name"] for t in agent.tools or []}


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --- tool surface ------------------------------------------------------------------------------------------------

def test_only_delegate_children_get_the_tools(hermes, family):
    hermes()
    parent = family.parent("p1")
    child = family.child(parent, "GOAL A")
    assert TOOLS <= tool_names(child)
    assert not TOOLS & tool_names(parent)


def test_tools_stay_direct_when_tool_search_defers_plugin_tools(hermes, family):
    hermes()  # tool_search defaults to "auto": every plugin tool is deferrable behind the bridge
    child = family.child(family.parent("p1"), "GOAL A")
    names = tool_names(child)
    assert TOOLS <= names, names


# --- siblings ----------------------------------------------------------------------------------------------------

def test_sibling_message_is_steered_into_the_running_sibling(hermes, family):
    hermes()
    parent = family.parent("p1")
    a, b = family.child(parent, "GOAL A", 0), family.child(parent, "GOAL B", 1)
    result = call(a, "subagent_send", to=b._subagent_id, message="schema is in docs/api.md")
    assert result["delivered"] == [b._subagent_id], result
    text = pending(b)
    assert "schema is in docs/api.md" in text
    assert a._subagent_id in text and "GOAL A" in text  # who sent it, and about what
    assert f'subagent_send(to="{a._subagent_id}"' in text  # how to answer


def test_broadcast_reaches_running_siblings_only(hermes, family):
    hermes()
    p1, p2 = family.parent("p1"), family.parent("p2", "agent:main:discord:thread:9")
    a, b = family.child(p1, "GOAL A", 0), family.child(p1, "GOAL B", 1)
    cousin = family.child(p2, "GOAL C", 0)
    result = call(a, "subagent_send", to="siblings", message="I own src/api; stay out")
    assert result["delivered"] == [b._subagent_id], result
    assert "stay out" in pending(b)
    assert pending(cousin) == ""
    assert pending(a) == ""  # never to itself


def test_a_child_cannot_message_another_parents_child(hermes, family):
    hermes()
    a = family.child(family.parent("p1"), "GOAL A")
    cousin = family.child(family.parent("p2", "agent:main:discord:thread:9"), "GOAL C")
    result = call(a, "subagent_send", to=cousin._subagent_id, message="hi")
    assert "error" in result and cousin._subagent_id in result["error"]
    assert pending(cousin) == ""


def test_a_finished_sibling_is_reported_not_running(hermes, family):
    hermes()
    parent = family.parent("p1")
    a, b = family.child(parent, "GOAL A", 0), family.child(parent, "GOAL B", 1)
    from tools.delegate_tool_registry import _close_subagent_steering
    _close_subagent_steering(b._subagent_id, b)  # what a finishing child does
    result = call(a, "subagent_send", to=b._subagent_id, message="too late")
    assert "error" in result and "not running" in result["error"]


def test_peers_lists_self_and_running_siblings(hermes, family):
    hermes()
    p1 = family.parent("p1")
    a, b = family.child(p1, "GOAL A", 0), family.child(p1, "GOAL B", 1)
    family.child(family.parent("p2", "agent:main:discord:thread:9"), "GOAL C")
    result = call(a, "subagent_peers")
    assert result["you"]["subagent_id"] == a._subagent_id
    assert [s["subagent_id"] for s in result["siblings"]] == [b._subagent_id]
    assert result["siblings"][0]["goal"] == "GOAL B"


# --- parent ------------------------------------------------------------------------------------------------------

def test_parent_message_wakes_the_parent_chat_session(hermes, family, gateway):
    hermes()
    parent = family.parent("p1")
    a = family.child(parent, "GOAL A")
    result = call(a, "subagent_send", to="parent", message="halfway: 3 of 6 files migrated")
    assert result["delivered"] == ["parent"], result
    assert wait_for(lambda: len(gateway) == 1)
    turn = gateway[0]
    assert turn["session_key"] == ROUTE and turn["plugin_id"] == PLUGIN_KEY
    assert "halfway: 3 of 6 files migrated" in turn["content"]
    assert a._subagent_id in turn["content"] and "GOAL A" in turn["content"]
    assert f'delegate_task(action="steer", subagent_id="{a._subagent_id}"' in turn["content"]
    assert pending(parent) == ""


def test_parent_messages_sent_together_arrive_as_one_turn(hermes, family, gateway):
    hermes()
    parent = family.parent("p1")
    a, b = family.child(parent, "GOAL A", 0), family.child(parent, "GOAL B", 1)
    call(a, "subagent_send", to="parent", message="A done with step 1")
    call(b, "subagent_send", to="parent", message="B blocked on credentials")
    assert wait_for(lambda: len(gateway) >= 1)
    time.sleep(0.5)
    assert len(gateway) == 1
    assert "A done with step 1" in gateway[0]["content"] and "B blocked on credentials" in gateway[0]["content"]


def test_parent_message_falls_back_to_steering_the_parent_without_gateway_injection(hermes, family, gateway):
    hermes(injection=False)
    parent = family.parent("p1")
    a = family.child(parent, "GOAL A")
    call(a, "subagent_send", to="parent", message="need a decision on the DB")
    assert wait_for(lambda: "need a decision on the DB" in (parent._pending_steer or ""))
    assert gateway == []


def test_parent_messages_per_child_are_capped(hermes, family, gateway):
    hermes({"max_parent_messages_per_child": 2})
    a = family.child(family.parent("p1"), "GOAL A")
    assert "delivered" in call(a, "subagent_send", to="parent", message="one")
    assert "delivered" in call(a, "subagent_send", to="parent", message="two")
    third = call(a, "subagent_send", to="parent", message="three")
    assert "error" in third and "final result" in third["error"]


def test_orchestrator_child_reaches_its_own_subagent_parent(hermes, family, gateway, monkeypatch):
    hermes()
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    orchestrator = family.child(family.parent("p1"), "ORCHESTRATE")
    worker = family.child(orchestrator, "WORK")
    call(worker, "subagent_send", to="parent", message="worker found the bug")
    assert "worker found the bug" in pending(orchestrator)
    time.sleep(0.3)
    assert gateway == []


# --- waiting -----------------------------------------------------------------------------------------------------

def _wait_in_thread(agent, seconds: float):
    box = {}
    thread = threading.Thread(target=lambda: box.update(call(agent, "subagent_wait", timeout_seconds=seconds)))
    thread.start()
    return thread, box


def test_wait_returns_when_a_sibling_message_arrives(hermes, family):
    hermes()
    parent = family.parent("p1")
    a, b = family.child(parent, "GOAL A", 0), family.child(parent, "GOAL B", 1)
    started = time.monotonic()
    thread, box = _wait_in_thread(b, 30)
    time.sleep(0.3)
    call(a, "subagent_send", to=b._subagent_id, message="your turn")
    thread.join(10)
    assert box.get("status") == "message_pending", box
    assert time.monotonic() - started < 5
    assert "your turn" in pending(b)  # delivered right after the wait's result


def test_wait_returns_when_the_parent_steers(hermes, family):
    hermes()
    b = family.child(family.parent("p1"), "GOAL B")
    thread, box = _wait_in_thread(b, 30)
    time.sleep(0.3)
    from tools.delegate_tool_registry import steer_subagent
    assert steer_subagent(b._subagent_id, "parent says: stop after step 2")
    thread.join(10)
    assert box.get("status") == "message_pending", box


def test_wait_times_out_and_keeps_the_child_alive(hermes, family):
    hermes()
    b = family.child(family.parent("p1"), "GOAL B")
    before = b.get_activity_summary()["last_activity_ts"]
    result = call(b, "subagent_wait", timeout_seconds=1)
    assert result["status"] == "timeout"
    assert b.get_activity_summary()["last_activity_ts"] > before


# --- dependencies ------------------------------------------------------------------------------------------------

def test_missing_registry_seam_warns_and_registers_no_tools(hermes, family, monkeypatch, caplog):
    import tools.delegate_tool_registry as registry_module
    monkeypatch.delattr(registry_module, "steer_subagent")
    with caplog.at_level(logging.WARNING):
        hermes()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "subagent-messaging" in r.getMessage()]
    assert len(warnings) == 1 and "steer_subagent" in warnings[0]
    child = family.child(family.parent("p1"), "GOAL A")
    assert not TOOLS & tool_names(child)
