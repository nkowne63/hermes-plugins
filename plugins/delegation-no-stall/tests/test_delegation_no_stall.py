"""Tests for the delegation-no-stall plugin.

Every test drives the core's OWN stall machinery from the pinned Hermes tree (``~/.hermes/hermes-agent``, see
scripts/test-plugin.sh): the per-child heartbeat (``tools.delegate_tool_child_run._Heartbeat``), the background
delegation monitor (``tools.async_delegation._sweep_stale_locked``) and the per-turn liveness watchdog
(``agent.turn_facade_lease.DurableTurnLease``). ``test_core_*`` prove each one fires without the plugin, so the
plugin tests pass only because the plugin switched them off.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"
DAY_OF_HEARTBEATS = 24 * 60 * 60 // 30  # the heartbeat ticks every 30 s
_AGENT_KW = dict(
    api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter", api_mode="chat_completions",
    model="anthropic/claude-sonnet-4.6", platform="cli", quiet_mode=True, skip_memory=True,
    save_trajectories=False, enabled_toolsets=["file"],
)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    if not HERMES_TREE.is_dir():
        pytest.skip(f"Hermes tree not found at {HERMES_TREE}")
    monkeypatch.syspath_prepend(str(HERMES_TREE))
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", f"/pytest-{tmp_path.name}")
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    return home


def _load_plugin():
    spec = importlib.util.spec_from_file_location("delegation_no_stall_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = SimpleNamespace(unload=[])
    ctx.on_unload = ctx.unload.append
    module.ctx = ctx
    return module


def _unload(module) -> None:
    for callback in module.ctx.unload:
        callback()


@pytest.fixture
def plugin():
    module = _load_plugin()
    module.register(module.ctx)
    yield module
    _unload(module)


# --- the per-child heartbeat (sync joins AND background units run it) -------------------------------------------

class _FrozenChild:
    """A child whose progress signals never move: same call count, same tool, same activity clock."""

    def __init__(self, tool=None):
        self.tool = tool

    def get_activity_summary(self):
        return {"current_tool": self.tool, "api_call_count": 3, "max_iterations": 1024, "last_activity_ts": 1000.0}


class _Parent:
    def __init__(self):
        self.touches = 0

    def _touch_activity(self, _desc):
        self.touches += 1


def _heartbeat_after(ticks: int, tool=None):
    from tools.delegate_tool_child_run import _Heartbeat
    parent = _Parent()
    heartbeat = _Heartbeat(_FrozenChild(tool), parent, 0)
    for _ in range(ticks):
        if heartbeat.tick() is False:  # the core's stale verdict stops the periodic callback
            break
    return heartbeat, parent


@pytest.mark.parametrize("tool", [None, "terminal"])
def test_core_heartbeat_abandons_a_frozen_child(tool):
    heartbeat, _ = _heartbeat_after(DAY_OF_HEARTBEATS, tool)
    assert heartbeat.settled.is_set()


@pytest.mark.parametrize("tool", [None, "terminal"])
def test_heartbeat_never_abandons_a_child_frozen_for_a_day(plugin, tool):
    heartbeat, parent = _heartbeat_after(DAY_OF_HEARTBEATS, tool)
    assert not heartbeat.settled.is_set()
    assert heartbeat.stale_threshold_seconds is None
    # It still keeps the parent's activity clock moving (what masks the gateway inactivity timeout).
    assert parent.touches == DAY_OF_HEARTBEATS


# --- the background (async) delegation monitor -------------------------------------------------------------------

def _sweep_frozen_delegation(quiet_for: float, in_tool: bool):
    import tools.async_delegation as ad
    now = time.time()
    token = ("frozen",)
    delegation_id = "deleg_no_stall_test"
    record = {
        "delegation_id": delegation_id, "status": "running", "_started": True,
        "progress_fn": lambda: (token, in_tool), "_progress_token": token, "_progress_ts": now - quiet_for,
        "_interrupted_at": None,
    }
    with ad._records_lock:
        ad._records[delegation_id] = record
        try:
            stalled, _expired, _monitorable = ad._sweep_stale_locked(now)
        finally:
            ad._records.pop(delegation_id, None)
    return [entry[0] for entry in stalled], record["status"]


@pytest.mark.parametrize("in_tool", [False, True])
def test_core_monitor_stalls_a_frozen_background_delegation(in_tool):
    stalled, status = _sweep_frozen_delegation(24 * 3600, in_tool)
    assert stalled == ["deleg_no_stall_test"]
    assert status == "stalling"


@pytest.mark.parametrize("in_tool", [False, True])
def test_monitor_never_stalls_a_background_delegation_frozen_for_a_day(plugin, in_tool):
    stalled, status = _sweep_frozen_delegation(24 * 3600, in_tool)
    assert stalled == []
    assert status == "running"


# --- the per-turn liveness watchdog -------------------------------------------------------------------------------

@pytest.fixture
def agents(monkeypatch):
    """A real top-level AIAgent and a real delegate_task child built from it (``_build_child_agent``)."""
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    import tools.delegate_tool_config as dtc
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dtc, "_load_config", dict)
    parent = AIAgent(**_AGENT_KW, session_id="parent-session")
    child = dt._build_child_agent(task_index=0, goal="CHILD GOAL", context=None, toolsets=["file"], model=None,
                                  max_iterations=4, task_count=1, parent_agent=parent)
    yield parent, child
    child.close()
    parent.close()


def _watchdog_for(agent):
    from agent.turn_facade_lease import DurableTurnLease
    lease = DurableTurnLease(agent, None, agent.session_id, "holder")
    lease.build_threads()
    return lease.watchdog


def test_core_liveness_watchdog_guards_child_turns(agents):
    _parent, child = agents
    assert _watchdog_for(child) is not None


def test_child_turns_get_no_liveness_watchdog(plugin, agents):
    parent, child = agents
    assert _watchdog_for(child) is None
    assert _watchdog_for(parent) is not None  # top-level turns keep theirs


# --- lifecycle ---------------------------------------------------------------------------------------------------

def test_unload_restores_every_stall_check(agents):
    module = _load_plugin()
    module.register(module.ctx)
    _unload(module)
    heartbeat, _ = _heartbeat_after(DAY_OF_HEARTBEATS)
    assert heartbeat.settled.is_set()
    assert _sweep_frozen_delegation(24 * 3600, False)[0] == ["deleg_no_stall_test"]
    assert _watchdog_for(agents[1]) is not None


def test_a_missing_seam_warns_once_and_the_others_still_apply(monkeypatch, caplog):
    import tools.async_delegation as ad
    monkeypatch.delattr(ad, "_STALE_IDLE_SECONDS")
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    try:
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "_STALE_IDLE_SECONDS" in warnings[0] and "still" in warnings[0]
        heartbeat, _ = _heartbeat_after(DAY_OF_HEARTBEATS)
        assert not heartbeat.settled.is_set()
        assert _sweep_frozen_delegation(24 * 3600, True)[0] == []  # the in-tool limit was still lifted
    finally:
        _unload(module)
