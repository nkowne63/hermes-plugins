"""Hermes delegation-no-stall plugin: delegate_task children are never stopped for "no progress".

Hermes stops a delegate_task child whose progress signals (API-call count, current tool, activity clock) stay
frozen for a while. Three independent checks do it; this plugin switches off each one, for children only:

1. The per-child heartbeat (``tools.delegate_tool_child_run._Heartbeat``) abandons the child after
   ``_HEARTBEAT_STALE_CYCLES_IDLE`` (15 x 30 s = 450 s) idle or ``_HEARTBEAT_STALE_CYCLES_IN_TOOL`` (40 x 30 s =
   1200 s) on one tool. Both are module globals of ``tools.delegate_tool`` that ``tick()`` re-imports on every
   tick, so they are set to "never". The heartbeat itself keeps running: it is what keeps the parent's activity
   clock moving (the gateway inactivity timeout would otherwise fire during a long synchronous join).
2. The background-delegation monitor (``tools.async_delegation._sweep_stale_locked``) interrupts a unit after
   ``_STALE_IDLE_SECONDS`` (450 s) / ``_STALE_IN_TOOL_SECONDS`` (1200 s) and force-finalizes it as ``stalled``
   120 s later. Both limits are read at call time and are set to infinity.
3. The turn-liveness watchdog (``agent.turn_liveness``, ``agent.turn_liveness.timeout_s``, default 600 s) aborts
   any turn whose activity clock froze; a child's whole run is one turn under its own session lease.
   ``DurableTurnLease.build_threads`` is wrapped to drop the watchdog when the lease's agent is a delegate child
   (``_delegate_depth > 0``). Top-level turns keep theirs.

Left alone on purpose: ``delegation.child_timeout_seconds`` (an explicit opt-in cap, off unless configured), the
per-API-call stale watchdog (it retries one model call, it does not stop the child), and every manual or lifecycle
stop (``delegate_task(action="stop")``, ``/stop``, ``/new``, gateway shutdown).

A seam that is missing or changed upstream is logged as one WARNING naming the check that stays active; the
other seams still apply. Unloading restores the core values.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import math
import sys
from typing import Any, Optional

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

# (module, global, value that means "never", the core check that stays active when the global is unusable)
_LIMITS = (
    ("tools.delegate_tool", "_HEARTBEAT_STALE_CYCLES_IDLE", sys.maxsize,
     "the heartbeat still abandons a child idle for 450 s"),
    ("tools.delegate_tool", "_HEARTBEAT_STALE_CYCLES_IN_TOOL", sys.maxsize,
     "the heartbeat still abandons a child stuck on one tool for 1200 s"),
    ("tools.async_delegation", "_STALE_IDLE_SECONDS", math.inf,
     "background delegations idle for 450 s are still stopped as stalled"),
    ("tools.async_delegation", "_STALE_IN_TOOL_SECONDS", math.inf,
     "background delegations stuck on one tool for 1200 s are still stopped as stalled"),
)
_LEASE_MODULE = "agent.turn_facade_lease"
_LEASE_CLASS = "DurableTurnLease"
_LEASE_LOST = "child turns are still aborted after agent.turn_liveness.timeout_s (default 600 s) without activity"
_WRAPPED_MARK = "_delegation_no_stall_wrapped"
_MISSING = object()

_originals: dict[tuple[str, str], Any] = {}  # (module, global) -> core value, while lifted
_registrations = 0  # live register() calls (one per profile loading the plugin in this process)


def _warn(seam: str, problem: str, lost: str) -> None:
    logger.warning("delegation-no-stall: %s %s, so %s. Update the plugin for this Hermes version.",
                   seam, problem, lost)


def _lift_limit(module_name: str, name: str, never: Any, lost: str) -> None:
    seam = f"{module_name}.{name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        _warn(seam, f"cannot be imported ({type(exc).__name__}: {exc})", lost)
        return
    current = getattr(module, name, _MISSING)
    if current is _MISSING:
        _warn(seam, "is missing", lost)
        return
    if (module_name, name) in _originals:
        return  # already lifted by another profile's registration
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        _warn(seam, f"is no longer a number ({current!r})", lost)
        return
    _originals[(module_name, name)] = current
    setattr(module, name, never)


def _is_delegate_child(agent: Any) -> bool:
    depth = getattr(agent, "_delegate_depth", 0)
    return isinstance(depth, int) and depth > 0


def _wrap_build_threads(original):
    @functools.wraps(original)
    def build_threads(self):
        original(self)
        if _is_delegate_child(getattr(self, "agent", None)):
            self.watchdog = None  # the state an unconfigured (timeout_s <= 0) watchdog leaves

    setattr(build_threads, _WRAPPED_MARK, original)
    return build_threads


def _install_lease_wrapper() -> Optional[str]:
    """Wrap ``DurableTurnLease.build_threads`` once; None when installed, else why the seam is unusable."""
    try:
        module = importlib.import_module(_LEASE_MODULE)
    except Exception as exc:
        return f"cannot be imported ({type(exc).__name__}: {exc})"
    lease_class = getattr(module, _LEASE_CLASS, None)
    if lease_class is None:
        return "is missing"
    current = getattr(lease_class, "build_threads", None)
    if getattr(current, _WRAPPED_MARK, None):
        return None
    if not callable(current):
        return "has no build_threads"
    try:
        params = list(inspect.signature(current).parameters)
    except (TypeError, ValueError):
        params = None
    if params != ["self"]:
        return f"build_threads signature changed ({params!r}, expected ['self'])"
    lease_class.build_threads = _wrap_build_threads(current)
    return None


def _uninstall_lease_wrapper() -> None:
    module = sys.modules.get(_LEASE_MODULE)
    lease_class = getattr(module, _LEASE_CLASS, None) if module is not None else None
    current = getattr(lease_class, "build_threads", None) if lease_class is not None else None
    original = getattr(current, _WRAPPED_MARK, None)
    if original is not None:
        lease_class.build_threads = original


def _restore_limits() -> None:
    for (module_name, name), value in _originals.items():
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, name, value)
    _originals.clear()


def _on_unload() -> None:
    global _registrations
    _registrations = max(0, _registrations - 1)
    if not _registrations:
        _restore_limits()
        _uninstall_lease_wrapper()


def register(ctx: Any) -> None:
    global _registrations
    _registrations += 1
    ctx.on_unload(_on_unload)
    for module_name, name, never, lost in _LIMITS:
        _lift_limit(module_name, name, never, lost)
    problem = _install_lease_wrapper()
    if problem is not None:
        _warn(f"{_LEASE_MODULE}.{_LEASE_CLASS}", problem, _LEASE_LOST)
    logger.info("delegation-no-stall v%s armed", __version__)


__all__ = ["register", "__version__"]
