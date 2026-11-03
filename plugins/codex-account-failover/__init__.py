"""Hermes codex-account-failover plugin: retry the same model on the other codex account before fallback_providers.

Hermes already rotates an ``openai-codex`` credential pool on credential failures (rate limit, billing, auth,
model entitlement: ``recover_with_credential_pool``). Every OTHER failure that ends a primary attempt — empty or
malformed responses, a reasoning-only stall, server errors, overload, timeouts, an unclassified error, a WAF
block — goes straight to ``fallback_providers`` (another provider and model) through
``AIAgent._try_activate_fallback``, although the next pool account would serve the same model.

This plugin wraps ``agent.chat_completion_helpers.try_activate_fallback``, the implementation every one of those
call sites reaches through the ``AIAgent._try_activate_fallback`` lazy forwarder (resolved on each call, so the
wrapper applies however early or late ``run_agent`` is imported). When ALL of these hold:

- the agent is on ``openai-codex`` and its credential pool belongs to that provider and endpoint;
- ``reason`` is None, ``incomplete_response``, ``server_error``, ``overloaded``, ``timeout``, ``unknown`` or
  ``upstream_blocked``, and a reason-less call does not come from the safety-refusal paths
  (``handle_content_policy_refusal`` / ``_content_filter_fallback``: content-deterministic);
- another pool entry is available for this model now (``CredentialPool.reclaim``: cooldowns, model-scoped
  cooldowns and token refresh as ``select`` applies them) and was not already used in this turn;

it swaps that entry in (``AIAgent._swap_credential``) and returns True, so the caller retries the same model on
the other account exactly as it would retry on a fallback provider. The failed account is NOT benched (other
sessions keep using it). When the account it left outranks the one it moved to, ``_credential_pool_revert_id``
is armed (unless already armed), so Hermes' own per-turn ``_revert_credential_rotation`` moves the session back at
the start of the next turn. Accounts used in a turn are tracked per ``AIAgent._current_turn_id``; once every
account was tried, or any condition fails, the original runs: the fallback chain is unchanged.

A missing or changed seam is logged as one WARNING naming the lost behaviour (see README.md).
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import sys
from typing import Any, Optional

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

_HELPERS_MODULE = "agent.chat_completion_helpers"
_SEAM = "try_activate_fallback"
_SEAM_PARAMS = ["agent", "reason", "reset_at"]
_SEAM_LOST = ("so non-credential codex failures go straight to fallback_providers without retrying the same "
              "model on the other codex account")
_PROVIDER = "openai-codex"
# FailoverReason values that justify the same model on another account (None: the reason-less call sites).
_RETRY_REASONS = frozenset({
    "incomplete_response", "server_error", "overloaded", "timeout", "unknown", "upstream_blocked",
})
# Reason-less call sites that are content-deterministic: another account would get the same refusal.
_REFUSAL_MODULE = "agent.turn_truncation"
_REFUSAL_CALLERS = ("handle_content_policy_refusal", "_content_filter_fallback")
_REFUSAL_LOST = ("so safety refusals reported without a reason also retry on the other codex account before "
                 "fallback_providers")
_CALLER_DEPTH = 3  # wrapper <- lazy forwarder <- call site (one spare frame)
_TRIED_ATTR = "_codex_account_failover_tried"  # (turn id, ids of the accounts used in that turn)
_WRAPPED_MARK = "_codex_account_failover_wrapped"

_refusal_codes: frozenset = frozenset()
_registrations = 0  # live register() calls (one per profile loading the plugin in this process)


def _warn(seam: str, problem: str, lost: str) -> None:
    logger.warning("codex-account-failover: %s %s, %s. Update the plugin for this Hermes version.",
                   seam, problem, lost)


def _called_from_refusal() -> bool:
    frame = sys._getframe(2)  # skip this helper and retry_on_other_account: start at the wrapper
    for _ in range(_CALLER_DEPTH):
        if frame is None:
            return False
        if frame.f_code in _refusal_codes:
            return True
        frame = frame.f_back
    return False


def _tried_this_turn(agent: Any) -> set:
    turn = getattr(agent, "_current_turn_id", None)
    state = getattr(agent, _TRIED_ATTR, None)
    if not (isinstance(state, tuple) and len(state) == 2 and state[0] == turn):
        state = (turn, set())
        setattr(agent, _TRIED_ATTR, state)
    return state[1]


def _label(entry: Any) -> str:
    return f"{getattr(entry, 'id', '?')} ({getattr(entry, 'label', '?')})"


def retry_on_other_account(agent: Any, reason: Any) -> bool:
    """Swap *agent* onto another available codex pool account for the same model; True when swapped."""
    reason_value = getattr(reason, "value", reason)
    if reason is not None and reason_value not in _RETRY_REASONS:
        return False
    if str(getattr(agent, "provider", "") or "").strip().lower() != _PROVIDER:
        return False
    pool = getattr(agent, "_credential_pool", None)
    if pool is None:
        return False
    from agent.credential_pool import credential_pool_entry_serves_endpoint, credential_pool_matches_provider
    base_url = getattr(agent, "base_url", None)
    if not credential_pool_matches_provider(pool, _PROVIDER, base_url=base_url):
        return False
    if reason is None and _called_from_refusal():
        return False
    current_id = getattr(agent, "_credential_pool_entry_id", None) or pool.entry_id_for_api_key(
        getattr(agent, "api_key", None))
    if not current_id:
        return False  # cannot tell which account failed
    tried = _tried_this_turn(agent)
    tried.add(current_id)
    entries = pool.entries()
    current = next((e for e in entries if e.id == current_id), None)
    model = getattr(agent, "model", None) or None
    for candidate in entries:
        if candidate.id in tried:
            continue
        entry = pool.reclaim(candidate.id, model=model)
        if entry is None or not credential_pool_entry_serves_endpoint(entry, base_url):
            continue
        tried.add(entry.id)
        if agent._swap_credential(entry) is False:
            continue
        revert_armed = False
        if (current is not None and current.priority < entry.priority
                and not getattr(agent, "_credential_pool_revert_id", None)):
            agent._credential_pool_revert_id = current_id
            revert_armed = True
        logger.info(
            "codex-account-failover: %s on %s via account %s — retrying the same model on account %s "
            "before fallback_providers%s",
            reason_value or "failed response", model, _label(current) if current else current_id, _label(entry),
            "; back to the first account next turn" if revert_armed else "",
        )
        return True
    return False


def _wrap(original):
    @functools.wraps(original)
    def try_activate_fallback(agent, reason=None, reset_at=None):
        try:
            if retry_on_other_account(agent, reason):
                return True
        except Exception:
            logger.warning("codex-account-failover: account retry failed; using fallback_providers", exc_info=True)
        return original(agent, reason, reset_at)

    setattr(try_activate_fallback, _WRAPPED_MARK, original)
    return try_activate_fallback


def _install_seam() -> Optional[str]:
    """Wrap the fallback helper once (idempotent); None when installed, else why the seam is unusable."""
    try:
        module = importlib.import_module(_HELPERS_MODULE)
    except Exception as exc:
        return f"cannot be imported ({type(exc).__name__}: {exc})"
    current = getattr(module, _SEAM, None)
    if getattr(current, _WRAPPED_MARK, None):
        return None
    if not callable(current):
        return "is missing"
    try:
        params = list(inspect.signature(current).parameters)
    except (TypeError, ValueError):
        params = None
    if params != _SEAM_PARAMS:
        return f"signature changed ({params!r}, expected {_SEAM_PARAMS!r})"
    agent_class = getattr(sys.modules.get("run_agent"), "AIAgent", None)  # checked when already imported
    if agent_class is not None:
        forwarder = getattr(agent_class, "_try_activate_fallback", None)
        try:
            target = inspect.getclosurevars(forwarder).nonlocals
        except (TypeError, ValueError):
            target = {}
        if (target.get("module"), target.get("name")) != (_HELPERS_MODULE, _SEAM):
            return "is no longer what AIAgent._try_activate_fallback forwards to"
    setattr(module, _SEAM, _wrap(current))
    return None


def _collect_refusal_callers() -> Optional[str]:
    global _refusal_codes
    try:
        module = importlib.import_module(_REFUSAL_MODULE)
    except Exception as exc:
        return f"cannot be imported ({type(exc).__name__}: {exc})"
    codes, missing = set(), []
    for name in _REFUSAL_CALLERS:
        code = getattr(getattr(module, name, None), "__code__", None)
        if code is None:
            missing.append(name)
        else:
            codes.add(code)
    _refusal_codes = frozenset(codes)
    return f"{', '.join(missing)} missing" if missing else None


def _uninstall_seam() -> None:
    module = sys.modules.get(_HELPERS_MODULE)
    current = getattr(module, _SEAM, None) if module is not None else None
    original = getattr(current, _WRAPPED_MARK, None)
    if original is not None:
        setattr(module, _SEAM, original)


def _on_unload() -> None:
    global _registrations
    _registrations = max(0, _registrations - 1)
    if not _registrations:
        _uninstall_seam()


def register(ctx: Any) -> None:
    global _registrations
    _registrations += 1
    ctx.on_unload(_on_unload)
    problem = _install_seam()
    if problem is not None:
        _warn(f"{_HELPERS_MODULE}.{_SEAM}", problem, _SEAM_LOST)
        return
    problem = _collect_refusal_callers()
    if problem is not None:
        _warn(_REFUSAL_MODULE, problem, _REFUSAL_LOST)
    logger.info("codex-account-failover v%s armed", __version__)


__all__ = ["register", "retry_on_other_account", "__version__"]
