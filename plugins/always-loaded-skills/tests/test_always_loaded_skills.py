"""Tests for the always-loaded-skills plugin.

Collection needs no Hermes import: the Hermes tree (``~/.hermes/hermes-agent``, see
scripts/test-plugin.sh) is imported lazily inside fixtures/tests. The end-to-end tests install the
plugin into a scratch profile home, let Hermes' real plugin discovery load it, build REAL AIAgents
(a top-level agent and a ``delegate_task`` child built by ``tools.delegate_tool._build_child_agent``)
and capture the provider kwargs of a real ``run_conversation`` turn. The tree under test may still
carry the fork's core ``skills.always_loaded`` (``agent.agent_init._inject_always_loaded_skills``);
it is switched off unless a test says otherwise, so these tests prove the PLUGIN delivers the skill.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"
PLUGIN_KEY = "always-loaded-skills"
BODY = "PINNED ALWAYS-LOADED CONTENT"
_AGENT_KW = dict(
    api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter", api_mode="chat_completions",
    model="anthropic/claude-sonnet-4.6", platform="cli", quiet_mode=True, skip_memory=True,
    save_trajectories=False, enabled_toolsets=["file"],  # no skills toolset on purpose
)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("always_loaded_skills_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin():
    return _load_plugin_module()


@pytest.fixture
def tree(monkeypatch):
    if not HERMES_TREE.is_dir():
        pytest.skip(f"Hermes tree not found at {HERMES_TREE}")
    monkeypatch.syspath_prepend(str(HERMES_TREE))
    return HERMES_TREE


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test.\n---\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def home(tree, tmp_path, monkeypatch):
    """Scratch profile home with one installed skill and the plugin installed + enabled;
    ``home(always_loaded, core=False)`` writes the config (``core`` keeps the fork's core injection)."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Keep pytest tmp roots out of the platform-default root (same guard as session-reset-policy).
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", f"/pytest-{tmp_path.name}")
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    _write_skill(hermes_home / "skills", "pinned-skill", BODY)
    (hermes_home / "plugins").mkdir(parents=True)
    (hermes_home / "plugins" / PLUGIN_KEY).symlink_to(PLUGIN_DIR, target_is_directory=True)

    def configure(always_loaded=None, *, core: bool = False, extra: dict | None = None):
        skills = {} if always_loaded is None else {"always_loaded": always_loaded}
        cfg = {**(extra or {}), "skills": skills, "plugins": {"enabled": [PLUGIN_KEY]}}
        (hermes_home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")  # JSON is valid YAML
        if not core:
            import agent.agent_init as agent_init
            monkeypatch.setattr(agent_init, "_inject_always_loaded_skills", lambda *_a, **_k: None, raising=False)
        from hermes_cli.plugins import _ensure_plugins_discovered
        manager = _ensure_plugins_discovered(force=True)
        loaded = manager._plugins.get(PLUGIN_KEY)
        assert loaded is not None and loaded.enabled and not loaded.error, f"plugin not loaded: {loaded!r}"
        return manager

    return configure


def _agent(**kw):
    from run_agent import AIAgent
    return AIAgent(**{**_AGENT_KW, **kw})


def _chat_response(content="ok"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="m", usage=None)


def _codex_response(text="ok"):
    return SimpleNamespace(
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8), status="completed", model="m")


def _first_request(agent, user_message="hi") -> dict:
    """Run one real turn and return the provider kwargs of its first API call."""
    captured: list[dict] = []
    responses_api = agent.api_mode == "codex_responses"

    def _capture(api_kwargs):
        captured.append(api_kwargs)
        return _codex_response() if responses_api else _chat_response()

    agent._interruptible_api_call = _capture
    agent._disable_streaming = True
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None
    result = agent.run_conversation(user_message)
    assert result.get("completed") is True, result
    assert captured, "no API call was made"
    return captured[0]


def _system_sent(agent, user_message="hi") -> str:
    """The system text a real turn sends: ``instructions`` on the Responses API, else the system message."""
    request = _first_request(agent, user_message)
    if agent.api_mode == "codex_responses":
        return request["instructions"]
    first = request["messages"][0]
    assert first["role"] == "system"
    content = first["content"]
    return content if isinstance(content, str) else "\n\n".join(p.get("text", "") for p in content)


# --- config parsing --------------------------------------------------------------------------------

class TestResolve:
    @pytest.mark.parametrize("raw,expected", [
        (["pinned-skill", " pinned-skill ", "other", ""], ["pinned-skill", "other"]),
        ("pinned-skill, other\nthird", ["pinned-skill", "other", "third"]),
        ([], []),
        (None, []),
        ({"pinned-skill": True}, []),
    ])
    def test_list_or_string_deduplicated(self, plugin, raw, expected):
        cfg = {"skills": {"always_loaded": raw}} if raw is not None else {"skills": {}}
        assert plugin.resolve_always_loaded_skills(cfg) == expected

    def test_missing_skills_block(self, plugin):
        assert plugin.resolve_always_loaded_skills({}) == []
        assert plugin.resolve_always_loaded_skills(None) == []


# --- wire shapes ----------------------------------------------------------------------------------

class TestInjectSystemText:
    def test_chat_string_system(self, plugin):
        req = {"messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "u"}]}
        assert plugin.inject_system_text(req, "SKILL", "chat_completions")
        assert req["messages"][0]["content"] == "SYS\n\nSKILL"
        assert req["messages"][1] == {"role": "user", "content": "u"}

    def test_chat_parts_extend_last_text_part_keeping_cache_control(self, plugin):
        parts = [{"type": "text", "text": "A"}, {"type": "text", "text": "B", "cache_control": {"type": "ephemeral"}}]
        req = {"messages": [{"role": "system", "content": parts}]}
        assert plugin.inject_system_text(req, "SKILL", "chat_completions")
        assert req["messages"][0]["content"][1] == {"type": "text", "text": "B\n\nSKILL",
                                                    "cache_control": {"type": "ephemeral"}}

    def test_chat_without_system_gets_one(self, plugin):
        req = {"messages": [{"role": "user", "content": "u"}]}
        assert plugin.inject_system_text(req, "SKILL", "chat_completions")
        assert req["messages"][0] == {"role": "system", "content": "SKILL"}

    def test_anthropic_system_blocks(self, plugin):
        req = {"system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}], "messages": []}
        assert plugin.inject_system_text(req, "SKILL", "anthropic_messages")
        assert req["system"][0]["text"] == "S\n\nSKILL" and "cache_control" in req["system"][0]
        bare = {"messages": [{"role": "user", "content": "u"}]}
        assert plugin.inject_system_text(bare, "SKILL", "anthropic_messages")
        assert bare["system"] == "SKILL" and bare["messages"] == [{"role": "user", "content": "u"}]

    def test_bedrock_text_before_cache_point(self, plugin):
        req = {"system": [{"text": "S"}, {"cachePoint": {"type": "default"}}]}
        assert plugin.inject_system_text(req, "SKILL", "bedrock_converse")
        assert req["system"] == [{"text": "S\n\nSKILL"}, {"cachePoint": {"type": "default"}}]

    def test_responses_instructions(self, plugin):
        req = {"instructions": "I", "input": []}
        assert plugin.inject_system_text(req, "SKILL", "codex_responses")
        assert req["instructions"] == "I\n\nSKILL"
        empty = {"instructions": "", "input": []}
        assert plugin.inject_system_text(empty, "SKILL", "codex_responses")
        assert empty["instructions"] == "SKILL"

    def test_unknown_shape_left_alone(self, plugin):
        req = {"prompt": "x"}
        assert plugin.inject_system_text(req, "SKILL", "some_plugin_mode") is False
        assert req == {"prompt": "x"}


# --- middleware against the real skill renderer ---------------------------------------------------

def _chat_request(system="SYS"):
    return {"model": "m", "messages": [{"role": "system", "content": system}, {"role": "user", "content": "u"}]}


class TestMiddleware:
    def test_empty_or_missing_list_is_noop(self, home, plugin):
        for always_loaded in (None, []):
            home(always_loaded)
            assert plugin.always_loaded_middleware(request=_chat_request(), session_id="s",
                                                   api_mode="chat_completions") is None

    def test_injects_once_and_renders_once_per_session(self, home, plugin, monkeypatch):
        home(["pinned-skill"])
        import agent.skill_commands as skill_commands
        calls = []
        real = skill_commands.build_preloaded_skills_prompt
        monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt",
                            lambda *a, **k: calls.append(a) or real(*a, **k))
        for _ in range(3):
            out = plugin.always_loaded_middleware(request=_chat_request(), session_id="s1", api_mode="chat_completions")
            system = out["request"]["messages"][0]["content"]
            assert system.startswith("SYS\n\n") and system.count(BODY) == 1
        assert len(calls) == 1  # byte-stable across a session's calls; SKILL.md read once

    def test_key_survives_without_core_default(self, home, plugin, monkeypatch):
        """Once the fork drops ``skills.always_loaded`` from DEFAULT_CONFIG, the config loader must still
        hand the user's key through (the plugin reads the same key the rpi config already has)."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        monkeypatch.delitem(DEFAULT_CONFIG["skills"], "always_loaded", raising=False)
        home(["pinned-skill"])
        out = plugin.always_loaded_middleware(request=_chat_request(), session_id="s", api_mode="chat_completions")
        assert out["request"]["messages"][0]["content"].count(BODY) == 1

    def test_already_preloaded_or_auto_loaded_is_not_duplicated(self, home, plugin):
        home(["pinned-skill"])
        first = plugin.always_loaded_middleware(request=_chat_request(), session_id="s", api_mode="chat_completions")
        carried = first["request"]["messages"][0]["content"]  # a preloaded block, as `hermes -s` / core would send
        assert plugin.always_loaded_middleware(request=_chat_request(carried), session_id="s",
                                               api_mode="chat_completions") is None
        auto = 'SYS\n\n[IMPORTANT: The "pinned-skill" skill is auto-loaded via config (skills.auto_load).]'
        assert plugin.always_loaded_middleware(request=_chat_request(auto), session_id="s",
                                               api_mode="chat_completions") is None

    def test_unknown_name_warns_once_and_known_still_loads(self, home, plugin, caplog):
        home(["no-such-skill", "pinned-skill"])
        with caplog.at_level(logging.WARNING):
            outs = [plugin.always_loaded_middleware(request=_chat_request(), session_id=sid, api_mode="chat_completions")
                    for sid in ("a", "b", "c")]
        assert all(o["request"]["messages"][0]["content"].count(BODY) == 1 for o in outs)
        warned = [r.getMessage() for r in caplog.records if "skills.always_loaded" in r.getMessage()]
        assert len(warned) == 1 and "no-such-skill" in warned[0]

    def test_only_unknown_names_is_noop(self, home, plugin):
        home(["no-such-skill"])
        assert plugin.always_loaded_middleware(request=_chat_request(), session_id="s",
                                               api_mode="chat_completions") is None

    def test_errors_never_break_the_call(self, home, plugin, monkeypatch):
        home(["pinned-skill"])
        import agent.skill_commands as skill_commands

        def _boom(*_a, **_k):
            raise RuntimeError("broken skill")

        monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt", _boom)
        assert plugin.always_loaded_middleware(request=_chat_request(), session_id="s",
                                               api_mode="chat_completions") is None


# --- end to end: plugin loaded by Hermes, real agents, real turns ------------------------------------

class TestEveryAgentGetsIt:
    def test_plugin_registers_llm_request_middleware(self, home):
        manager = home(["pinned-skill"])
        callbacks = manager._middleware.get("llm_request", [])
        assert any(getattr(cb, "__name__", "") == "always_loaded_middleware" for cb in callbacks)

    def test_top_level_agent_without_skills_toolset(self, home):
        home(["pinned-skill"])
        agent = _agent(ephemeral_system_prompt="CALLER PROMPT")
        try:
            assert BODY not in (agent.ephemeral_system_prompt or "")  # core injection is off: the plugin does it
            system = _system_sent(agent)
            assert system.count(BODY) == 1
            assert "CALLER PROMPT" in system
        finally:
            agent.close()

    def test_delegate_child_gets_it(self, home, monkeypatch):
        home(["pinned-skill"])
        from tools import delegate_tool as dt
        import tools.delegate_tool_config as dtc
        monkeypatch.setattr(dt, "_load_config", dict)
        monkeypatch.setattr(dtc, "_load_config", dict)
        monkeypatch.setattr(dt, "_build_child_system_prompt", lambda *_a, **_kw: "DELEGATE CHILD PROMPT")
        parent = _agent(session_id="parent")
        try:
            child = dt._build_child_agent(task_index=0, goal="CHILD GOAL", context=None, toolsets=["file"],
                                          model=None, max_iterations=4, task_count=1, parent_agent=parent)
            try:
                # The child skips context files (skills.auto_load's gate) yet its model calls carry the skill once.
                assert child.skip_context_files is True
                system = _system_sent(child, "CHILD GOAL")
                assert system.count(BODY) == 1
                assert "DELEGATE CHILD PROMPT" in system
            finally:
                child.close()
        finally:
            parent.close()

    def test_core_injection_present_is_not_duplicated(self, home):
        """Against a tree that still has the core feature (or a fork that inherited the parent's ephemeral
        prompt), the block already in the system text is not sent twice."""
        home(["pinned-skill"], core=True)
        import agent.agent_init as agent_init
        if not hasattr(agent_init, "_inject_always_loaded_skills"):
            pytest.skip("tree has no core skills.always_loaded")
        agent = _agent()
        fork = _agent(ephemeral_system_prompt=agent.ephemeral_system_prompt)
        try:
            assert _system_sent(agent).count(BODY) == 1
            assert _system_sent(fork).count(BODY) == 1
        finally:
            fork.close()
            agent.close()

    @pytest.mark.parametrize("always_loaded", [None, []])
    def test_empty_or_missing_list_is_noop(self, home, always_loaded):
        home(always_loaded)
        agent = _agent(ephemeral_system_prompt="CALLER PROMPT")
        try:
            system = _system_sent(agent)
            assert BODY not in system and "skill preloaded" not in system
        finally:
            agent.close()


# --- the rpi's runtime: openai-codex main model and delegation, api_mode unset ----------------------

_RPI_LIKE = {
    "model": {"provider": "openai-codex", "default": "gpt-6.1-sol"},
    "delegation": {"provider": "openai-codex", "model": "gpt-6-luna"},
    "fallback_providers": [{"provider": "anthropic", "model": "claude-sonnet-4.6"}],
}


@pytest.fixture
def rpi_like(home, monkeypatch):
    """rpi-like config (no ``model.openai_runtime``) with a stub Codex OAuth login, so the REAL runtime
    ladder resolves openai-codex (CLI/gateway startup and delegation.provider both go through it)."""
    home(["pinned-skill"], extra=_RPI_LIKE)
    import hermes_cli.runtime_provider as rp
    monkeypatch.setattr(rp, "resolve_codex_runtime_credentials",
                        lambda *a, **k: {"api_key": "codex-token", "base_url": rp.DEFAULT_CODEX_BASE_URL})
    runtime = rp.resolve_runtime_provider(requested="openai-codex", target_model="gpt-6.1-sol")
    assert runtime["provider"] == "openai-codex" and runtime["api_key"] == "codex-token"
    return runtime


def _codex_agent(runtime, **kw):
    from run_agent import AIAgent
    return AIAgent(**{**_AGENT_KW, "provider": runtime["provider"], "base_url": runtime["base_url"],
                      "api_key": runtime["api_key"], "api_mode": None, "model": "gpt-6.1-sol", **kw})


class TestRpiOpenAICodex:
    def test_runtime_is_codex_responses_not_app_server(self, rpi_like):
        # codex_app_server is opt-in only (model.openai_runtime: codex_app_server); unset -> Responses API,
        # which runs the agent loop and therefore the llm_request middleware.
        assert rpi_like["api_mode"] == "codex_responses"

    def test_top_level_codex_agent_gets_it_in_instructions(self, rpi_like):
        agent = _codex_agent(rpi_like)  # api_mode unset, as on the rpi
        try:
            assert agent.api_mode == "codex_responses"
            assert _system_sent(agent).count(BODY) == 1
        finally:
            agent.close()

    def test_codex_delegate_child_gets_it_in_instructions(self, rpi_like, monkeypatch):
        from tools import delegate_tool as dt
        monkeypatch.setattr(dt, "_build_child_system_prompt", lambda *_a, **_kw: "DELEGATE CHILD PROMPT")
        parent = _codex_agent(rpi_like, session_id="parent")
        try:
            # delegate_task's own routing: delegation.provider/model from the real config, credentials through
            # the real runtime resolver, then the same _build_child_agent kwargs _build_children passes.
            routes, err = dt._routes_for([{"goal": "CHILD GOAL"}], dt._load_config(), None, parent)
            assert err is None and not routes[0].error, (err, routes)
            route = routes[0]
            child = dt._build_child_agent(task_index=0, goal="CHILD GOAL", context=None, toolsets=["file"],
                                          model=route.creds["model"], max_iterations=4, task_count=1,
                                          parent_agent=parent, **dt._route_overrides(route))
            try:
                assert (child.provider, child.model, child.api_mode) == ("openai-codex", "gpt-6-luna", "codex_responses")
                assert child.skip_context_files is True
                system = _system_sent(child, "CHILD GOAL")
                assert system.count(BODY) == 1 and "DELEGATE CHILD PROMPT" in system
            finally:
                child.close()
        finally:
            parent.close()
