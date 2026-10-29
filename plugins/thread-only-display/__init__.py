"""Hermes thread-only-display plugin: ``display.thread_only_channels``.

For a source whose ``chat_id`` is listed in ``display.thread_only_channels`` and that is not a thread
(no ``thread_id``), a turn shows only the final answer: tool progress off (except ``log``, which
writes a file, not chat), interim assistant messages off, thinking progress off, and the
``show_reasoning`` block is not prepended. Threads under a listed channel are unaffected. The list
accepts a YAML list, an int, or a comma-separated string.

Seams (private gateway methods, wrapped on their defining mixins so ``GatewayRunner`` inherits them):

* ``GatewayTurnMixin._run_agent_display_settings(self, source)`` — the returned ``_RunAgentDisplay``
  dataclass is rewritten with ``dataclasses.replace`` (``needs_progress_queue`` recomputed).
* ``GatewayTurnPersistenceMixin._hmwa_prepend_reasoning(self, agent_result, response, source,
  _intentional_silence)`` — skipped (response returned unchanged) for a listed channel.

The wraps are installed only inside a gateway process: at ``register`` when ``gateway.run`` is
already imported, else lazily on the first ``pre_gateway_dispatch`` (which every inbound turn passes
before its display settings are resolved). A CLI process never imports the gateway because of this
plugin. A missing/re-signed seam is logged as ONE WARNING naming the lost behavior.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import logging
import sys
from typing import Any, Optional

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

_WRAPPED_MARK = "_thread_only_display_wrapped"
_DISPLAY_FIELDS = (
    "progress_mode", "tool_progress_enabled", "interim_assistant_messages_enabled",
    "_thinking_enabled", "_native_slack_task_cards", "needs_progress_queue", "user_config",
)
_registrations = 0
_warned: set[str] = set()


def is_thread_only_display_channel(user_config: Any, source: Any) -> bool:
    """True when *source* is a channel listed in ``display.thread_only_channels`` and not a thread."""
    if getattr(source, "thread_id", None):
        return False
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    display = user_config.get("display") if isinstance(user_config, dict) else None
    configured = display.get("thread_only_channels") if isinstance(display, dict) else None
    if not chat_id or not configured:
        return False
    if isinstance(configured, (str, int)):
        configured = str(configured).split(",")
    if not isinstance(configured, (list, tuple, set)):
        return False
    return chat_id in {str(value).strip() for value in configured}


def suppress_transient_display(disp: Any) -> Any:
    """*disp* (a ``_RunAgentDisplay``) with chat-visible transient output turned off."""
    progress_mode = disp.progress_mode if disp.progress_mode == "log" else "off"
    tool_progress_enabled = False
    thinking = False
    return dataclasses.replace(
        disp, progress_mode=progress_mode, tool_progress_enabled=tool_progress_enabled,
        interim_assistant_messages_enabled=False, _thinking_enabled=thinking,
        needs_progress_queue=bool(tool_progress_enabled or thinking or disp._native_slack_task_cards),
    )


def _wrap_display_settings(original):
    @functools.wraps(original)
    def _run_agent_display_settings(self, source):
        disp = original(self, source)
        try:
            if is_thread_only_display_channel(getattr(disp, "user_config", None), source):
                return suppress_transient_display(disp)
        except Exception:
            logger.debug("thread-only-display: display rewrite failed", exc_info=True)
        return disp

    setattr(_run_agent_display_settings, _WRAPPED_MARK, original)
    return _run_agent_display_settings


def _wrap_prepend_reasoning(original):
    @functools.wraps(original)
    def _hmwa_prepend_reasoning(self, agent_result, response, source, _intentional_silence):
        try:
            from gateway.run import _load_gateway_config
            if is_thread_only_display_channel(_load_gateway_config(), source):
                return response
        except Exception:
            logger.debug("thread-only-display: reasoning check failed", exc_info=True)
        return original(self, agent_result, response, source, _intentional_silence)

    setattr(_hmwa_prepend_reasoning, _WRAPPED_MARK, original)
    return _hmwa_prepend_reasoning


# (module, class, method, expected params, wrapper factory, lost behavior)
_SEAMS = (
    ("gateway.run_turn", "GatewayTurnMixin", "_run_agent_display_settings", ["self", "source"],
     _wrap_display_settings,
     "tool progress / interim assistant messages / thinking progress will show in "
     "thread_only_channels"),
    ("gateway.run_turn_persistence", "GatewayTurnPersistenceMixin", "_hmwa_prepend_reasoning",
     ["self", "agent_result", "response", "source", "_intentional_silence"],
     _wrap_prepend_reasoning,
     "show_reasoning blocks will be prepended in thread_only_channels"),
)


def _check_display_dataclass() -> Optional[str]:
    runner = getattr(sys.modules.get("gateway.run"), "GatewayRunner", None)
    display_cls = getattr(runner, "_RunAgentDisplay", None)
    if display_cls is None or not dataclasses.is_dataclass(display_cls):
        return "GatewayRunner._RunAgentDisplay is missing or not a dataclass"
    names = {field.name for field in dataclasses.fields(display_cls)}
    missing = [name for name in _DISPLAY_FIELDS if name not in names]
    return f"GatewayRunner._RunAgentDisplay lacks fields {missing!r}" if missing else None


def _install_seam(module_name, class_name, method, params, factory) -> Optional[str]:
    """Wrap one seam (idempotent). None when installed, else why the private target is unusable."""
    import importlib
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        return f"{module_name} not importable ({exc})"
    cls = getattr(module, class_name, None)
    if cls is None:
        return f"{module_name}.{class_name} is missing"
    current = cls.__dict__.get(method)
    if getattr(current, _WRAPPED_MARK, None):
        return None
    if not callable(current):
        return f"{class_name}.{method} is missing"
    try:
        actual = list(inspect.signature(current).parameters)
    except (TypeError, ValueError):
        actual = None
    if actual != params:
        return f"{class_name}.{method} signature changed ({actual!r}, expected {params!r})"
    setattr(cls, method, factory(current))
    return None


def _warn_once(key: str, problem: str, lost: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "thread-only-display: %s NOT installed: %s. Lost behavior: %s. Update the plugin for this "
        "Hermes version.", key, problem, lost)


def install() -> None:
    """Install both wraps; requires the gateway (``gateway.run``) to be loaded in this process."""
    if "gateway.run" not in sys.modules:
        return
    for module_name, class_name, method, params, factory, lost in _SEAMS:
        problem = _install_seam(module_name, class_name, method, params, factory)
        if problem is None and method == "_run_agent_display_settings":
            problem = _check_display_dataclass()
            if problem is not None:
                _uninstall_seam(module_name, class_name, method)
        if problem is not None:
            _warn_once(f"{class_name}.{method}", problem, lost)


def _uninstall_seam(module_name, class_name, method) -> None:
    cls = getattr(sys.modules.get(module_name), class_name, None)
    original = getattr(cls.__dict__.get(method) if cls is not None else None, _WRAPPED_MARK, None)
    if original is not None:
        setattr(cls, method, original)


def uninstall() -> None:
    for module_name, class_name, method, *_rest in _SEAMS:
        _uninstall_seam(module_name, class_name, method)


async def _pre_gateway_dispatch(**_kwargs: Any) -> None:
    if _registrations:
        install()
    return None


def _on_unload() -> None:
    global _registrations
    _registrations = max(0, _registrations - 1)
    if not _registrations:
        uninstall()


def register(ctx: Any) -> None:
    global _registrations
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    _registrations += 1
    ctx.on_unload(_on_unload)
    install()
    logger.info("thread-only-display v%s armed", __version__)


__all__ = ["register", "install", "uninstall", "is_thread_only_display_channel", "__version__"]
