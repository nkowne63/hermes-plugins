"""Tests for the codex-account-failover plugin.

Every test drives a real ``AIAgent`` from the pinned Hermes tree (``~/.hermes/hermes-agent``, see
scripts/test-plugin.sh) on ``openai-codex`` with a real two-account ``CredentialPool`` (prolite at priority 0,
sci at priority 1) and the rpi fallback chain (anthropic opus -> sonnet). Only the network call is faked: the
codex Responses call answers per account key, the anthropic call answers as the fallback model. The
``test_core_*`` controls prove Hermes goes straight to anthropic without the plugin, so the plugin tests pass
only because the plugin retried the other codex account first.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HERMES_TREE = Path.home() / ".hermes" / "hermes-agent"
CODEX_BASE = "https://chatgpt.com/backend-api/codex"
MODEL = "gpt-5-codex"
PROLITE, SCI = "52351a00prolite", "ea575a00sci"
TOKENS = {PROLITE: "codex-token-prolite-1234567890", SCI: "codex-token-sci-1234567890"}
FALLBACKS = [
    {"provider": "anthropic", "model": "claude-opus-5-5"},
    {"provider": "anthropic", "model": "claude-sonnet-5-5"},
]


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
    # The fallback chain binds anthropic from the environment, offline.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-key-1234567890")
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    import time as _time
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    return home


def _load_plugin():
    spec = importlib.util.spec_from_file_location("codex_account_failover_under_test", PLUGIN_DIR / "__init__.py")
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


# --- a real codex agent with a two-account pool ----------------------------------------------------------------

def _codex_entry(entry_id, label, priority):
    from agent.credential_pool import PooledCredential
    return PooledCredential(
        provider="openai-codex", id=entry_id, label=label, auth_type="api_key", priority=priority,
        source="manual", access_token=TOKENS[entry_id], base_url=CODEX_BASE,
    )


def _build_agent(monkeypatch, *, provider="openai-codex", pool=None, base_url=CODEX_BASE,
                 api_mode="codex_responses", model=MODEL):
    from agent.credential_pool import CredentialPool
    from run_agent import AIAgent
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    if pool is None:
        pool = CredentialPool("openai-codex", [_codex_entry(PROLITE, "prolite", 0), _codex_entry(SCI, "sci", 1)])
    first = pool.select(model=model)
    agent = AIAgent(
        model=model, provider=provider, api_mode=api_mode, base_url=base_url, api_key=first.runtime_api_key,
        credential_pool=pool, fallback_model=FALLBACKS, quiet_mode=True, max_iterations=6,
        skip_context_files=True, skip_memory=True, save_trajectories=False,
    )
    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    agent._credential_pool_entry_id = first.id
    return agent, pool


def _codex_text(text):
    return SimpleNamespace(
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8), status="completed", model=MODEL,
    )


def _codex_empty():
    return SimpleNamespace(
        output=[], output_text="", usage=SimpleNamespace(input_tokens=5, output_tokens=0, total_tokens=5),
        status="completed", model=MODEL,
    )


def _anthropic_text(text, model):
    return SimpleNamespace(
        id="msg_1", type="message", role="assistant", model=model, stop_reason="end_turn", stop_sequence=None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
    )


class _Wire:
    """Fakes the network call only. Codex answers per account key (``replies[entry_id]`` is a list of
    responses, the last one repeating); anthropic answers as the fallback model. Records every call."""

    def __init__(self, agent, replies):
        self.agent = agent
        self.replies = {key: list(value) for key, value in replies.items()}
        self.calls: list[tuple[str, str, str]] = []
        by_key = {token: entry_id for entry_id, token in TOKENS.items()}

        def call(api_kwargs, *args, **kwargs):
            provider, model, key = agent.provider, agent.model, agent.api_key
            self.calls.append((provider, model, by_key.get(key, key)))
            if provider == "anthropic":
                return _anthropic_text(f"from {model}", model)
            queue = self.replies[by_key[key]]
            return (queue.pop(0) if len(queue) > 1 else queue[0])()

        agent._interruptible_api_call = call
        agent._interruptible_streaming_api_call = call

    def accounts(self, provider="openai-codex"):
        return [account for prov, _model, account in self.calls if prov == provider]

    def providers(self):
        return [prov for prov, _model, _account in self.calls]


def _entry_status(pool, entry_id):
    return next(e for e in pool.entries() if e.id == entry_id).last_status


# --- control: what Hermes does on its own -----------------------------------------------------------------------

def test_core_empty_response_goes_straight_to_fallback_providers(monkeypatch):
    agent, _pool = _build_agent(monkeypatch)
    wire = _Wire(agent, {PROLITE: [_codex_empty], SCI: [lambda: _codex_text("from sci")]})

    result = agent.run_conversation("hello")

    assert result["final_response"] == "from claude-opus-5-5"
    assert SCI not in wire.accounts()
    assert agent.provider == "anthropic"


# --- (a) same model on the other account first --------------------------------------------------------------------

def test_empty_response_retries_the_same_model_on_the_other_account(plugin, monkeypatch):
    agent, pool = _build_agent(monkeypatch)
    wire = _Wire(agent, {PROLITE: [_codex_empty], SCI: [lambda: _codex_text("from sci")]})

    result = agent.run_conversation("hello")

    assert result["final_response"] == "from sci"
    assert "anthropic" not in wire.providers()
    assert wire.accounts()[-1] == SCI and set(wire.accounts()) == {PROLITE, SCI}
    assert {model for _p, model, _a in wire.calls} == {MODEL}
    assert (agent.provider, agent.model, agent.api_key) == ("openai-codex", MODEL, TOKENS[SCI])
    assert agent._credential_pool_entry_id == SCI
    # Not a provider fallback: the chain is untouched and the failed account is not benched.
    assert agent._fallback_activated is False and agent._fallback_index == 0
    assert _entry_status(pool, PROLITE) is None
    assert agent._credential_pool_revert_id == PROLITE


def test_the_seam_returns_true_without_calling_the_fallback_chain(plugin, monkeypatch):
    agent, _pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.model, agent.api_key) == ("openai-codex", MODEL, TOKENS[SCI])
    assert agent._fallback_index == 0


# --- (b) second failure in the same turn -> fallback_providers ----------------------------------------------------

def test_both_accounts_failing_in_one_turn_falls_back_to_anthropic(plugin, monkeypatch):
    agent, _pool = _build_agent(monkeypatch)
    wire = _Wire(agent, {PROLITE: [_codex_empty], SCI: [_codex_empty]})

    result = agent.run_conversation("hello")

    assert result["final_response"] == "from claude-opus-5-5"
    codex = wire.accounts()
    assert SCI in codex and codex.index(SCI) > codex.index(PROLITE)
    assert PROLITE not in codex[codex.index(SCI):]  # never back to prolite within the turn
    assert wire.providers()[-1] == "anthropic"
    assert agent.provider == "anthropic" and agent.model == "claude-opus-5-5"


def test_second_seam_call_in_the_same_turn_runs_the_fallback_chain(plugin, monkeypatch):
    agent, _pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback() is True
    assert agent.api_key == TOKENS[SCI]
    assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.model) == ("anthropic", "claude-opus-5-5")


# --- (c) next turn reverts to prolite; the tried set resets -------------------------------------------------------

def test_next_turn_reverts_to_the_preferred_account_and_may_fail_over_again(plugin, monkeypatch):
    agent, pool = _build_agent(monkeypatch)
    wire = _Wire(agent, {PROLITE: [_codex_empty], SCI: [lambda: _codex_text("from sci")]})
    first = agent.run_conversation("hello")
    assert first["final_response"] == "from sci"

    wire.calls.clear()
    wire.replies[PROLITE] = [lambda: _codex_text("from prolite")]
    second = agent.run_conversation("again", conversation_history=first["messages"])

    assert second["final_response"] == "from prolite"
    assert wire.accounts() == [PROLITE]
    assert agent._credential_pool_entry_id == PROLITE and agent.api_key == TOKENS[PROLITE]
    assert agent._credential_pool_revert_id is None

    wire.calls.clear()
    wire.replies[PROLITE] = [_codex_empty]
    third = agent.run_conversation("once more", conversation_history=second["messages"])

    assert third["final_response"] == "from sci"
    assert "anthropic" not in wire.providers()


def test_moving_up_to_the_preferred_account_arms_no_revert(plugin, monkeypatch):
    """A session already on sci (e.g. started while prolite was benched) that moves UP to prolite must not be
    pulled back down to sci next turn."""
    agent, _pool = _build_agent(monkeypatch)
    agent.api_key, agent._credential_pool_entry_id = TOKENS[SCI], SCI
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.api_key) == ("openai-codex", TOKENS[PROLITE])
    assert getattr(agent, "_credential_pool_revert_id", None) is None


# --- (d) excluded reasons pass through ----------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "context_overflow", "payload_too_large", "format_error", "content_policy_blocked", "model_not_found",
    "rate_limit", "billing", "auth", "model_entitlement",
])
def test_excluded_reasons_go_to_the_fallback_chain(plugin, monkeypatch, reason):
    from agent.error_classifier import FailoverReason
    agent, _pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback(reason=FailoverReason[reason]) is True
    assert (agent.provider, agent.model) == ("anthropic", "claude-opus-5-5")


@pytest.mark.parametrize("reason", [
    None, "incomplete_response", "server_error", "overloaded", "timeout", "unknown", "upstream_blocked",
])
def test_included_reasons_retry_on_the_other_account(plugin, monkeypatch, reason):
    from agent.error_classifier import FailoverReason
    agent, _pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback(reason=FailoverReason[reason] if reason else None) is True
    assert (agent.provider, agent.api_key) == ("openai-codex", TOKENS[SCI])


def test_a_safety_refusal_goes_to_the_fallback_chain(plugin, monkeypatch):
    """The refusal path calls the seam without a reason; it is content-deterministic, so no account retry."""
    from agent.turn_retry_state import TurnRetryState
    from agent.turn_truncation import handle_content_policy_refusal
    agent, _pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"
    refusal = SimpleNamespace(
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="I can't.")])],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8), status="completed", model=MODEL,
    )

    verdict = handle_content_policy_refusal(
        agent, refusal, TurnRetryState(), thinking_spinner=None, messages=[], api_messages=[], api_kwargs={},
        active_system_prompt=None, conversation_history=None, api_call_count=1, effective_task_id="t",
        turn_id="turn-1", api_request_id="r", api_start_time=0.0, retry_count=0, max_retries=3,
    )

    assert verdict.action == "break"
    assert (agent.provider, agent.model) == ("anthropic", "claude-opus-5-5")


def test_a_cooling_down_other_account_is_not_used(plugin, monkeypatch):
    agent, pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"
    pool.mark_exhausted_and_rotate(status_code=429, credential_id=SCI, failure_reason="rate_limit",
                                   error_context={"message": "usage limit"})

    assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.model) == ("anthropic", "claude-opus-5-5")


def test_a_model_scoped_cooldown_on_the_other_account_is_respected(plugin, monkeypatch):
    agent, pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"
    pool.mark_exhausted_and_rotate(status_code=403, credential_id=SCI, failure_reason="model_entitlement",
                                   error_context={"message": "model not available"}, model=MODEL)
    assert not pool.reclaim(SCI, model=MODEL)  # precondition: sci is benched for this model

    assert agent._try_activate_fallback() is True
    assert agent.provider == "anthropic"


# --- (e) other providers pass through -----------------------------------------------------------------------------

def test_a_non_codex_provider_goes_to_the_fallback_chain(plugin, monkeypatch):
    from agent.credential_pool import CredentialPool, PooledCredential
    base = "https://openrouter.ai/api/v1"
    pool = CredentialPool("openrouter", [
        PooledCredential(provider="openrouter", id=f"or-{i}", label=f"or-{i}", auth_type="api_key", priority=i,
                         source="manual", access_token=f"sk-or-test-{i}-1234567890", base_url=base)
        for i in range(2)
    ])
    agent, _pool = _build_agent(monkeypatch, provider="openrouter", pool=pool, base_url=base,
                                api_mode="chat_completions", model="openai/gpt-5")
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.model) == ("anthropic", "claude-opus-5-5")


def test_a_single_account_pool_goes_to_the_fallback_chain(plugin, monkeypatch):
    from agent.credential_pool import CredentialPool
    agent, _pool = _build_agent(monkeypatch, pool=CredentialPool("openai-codex", [_codex_entry(PROLITE, "prolite", 0)]))
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback() is True
    assert agent.provider == "anthropic"


# --- (f) seams: startup check, unload ------------------------------------------------------------------------------

def test_registers_on_this_hermes_without_a_warning(caplog):
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    try:
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    finally:
        _unload(module)


def test_a_missing_seam_warns_once_naming_the_lost_behaviour(monkeypatch, caplog):
    import agent.chat_completion_helpers as helpers
    monkeypatch.delattr(helpers, "try_activate_fallback")
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    _unload(module)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "try_activate_fallback" in warnings[0] and "other codex account" in warnings[0]


def test_a_changed_seam_signature_warns(monkeypatch, caplog):
    import agent.chat_completion_helpers as helpers
    monkeypatch.setattr(helpers, "try_activate_fallback", lambda agent, why: False)
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    _unload(module)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "signature" in warnings[0]


def test_a_missing_refusal_caller_warns(monkeypatch, caplog):
    import agent.turn_truncation as truncation
    monkeypatch.delattr(truncation, "handle_content_policy_refusal")
    module = _load_plugin()
    with caplog.at_level(logging.WARNING):
        module.register(module.ctx)
    _unload(module)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "handle_content_policy_refusal" in warnings[0]


def test_the_agent_method_still_forwards_to_the_wrapped_helper():
    """The seam this plugin wraps is what every call site reaches through ``AIAgent._try_activate_fallback``."""
    import inspect
    import agent.chat_completion_helpers as helpers
    from run_agent import AIAgent
    assert list(inspect.signature(helpers.try_activate_fallback).parameters) == ["agent", "reason", "reset_at"]
    sentinel = object()
    original = helpers.try_activate_fallback
    try:
        helpers.try_activate_fallback = lambda agent, reason=None, reset_at=None: sentinel
        assert AIAgent._try_activate_fallback(SimpleNamespace()) is sentinel
    finally:
        helpers.try_activate_fallback = original


def test_unload_restores_the_core_behaviour(monkeypatch):
    module = _load_plugin()
    module.register(module.ctx)
    _unload(module)
    agent, _pool = _build_agent(monkeypatch)
    agent._current_turn_id = "turn-1"

    assert agent._try_activate_fallback() is True
    assert agent.provider == "anthropic"
