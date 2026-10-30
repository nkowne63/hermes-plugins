"""Hermes subagent-messaging plugin: running delegate_task subagents talk without finishing.

Hermes gives a parent ``delegate_task(action="steer")`` to talk to a running child, but a child can only speak
through its final result, and siblings cannot reach each other at all. This plugin adds three tools that exist
only inside delegate_task children:

- ``subagent_peers``: the caller's own ``subagent_id`` and its running siblings (same parent conversation).
- ``subagent_send(to, message)``: ``to`` is ``"parent"``, a sibling's ``subagent_id`` or ``"siblings"``.
- ``subagent_wait(timeout_seconds)``: block until a message for the caller is queued (or the timeout passes).

Delivery
--------
- To a sibling: ``tools.delegate_tool_registry.steer_subagent`` — the text lands in the sibling's running turn
  after its current step, exactly like a parent's ``delegate_task(action="steer")``.
- To the parent when the parent is itself a subagent (orchestrator): the same in-process steer.
- To a top-level parent: messages to one parent conversation within ``_PARENT_WINDOW_SECONDS`` are joined and
  sent as ONE new turn of the parent's chat session through ``ctx.inject_message(session_key=...)`` (the gateway
  commits it to the session's durable FIFO, so a busy parent gets it right after its current turn). Needs
  ``plugins.entries.subagent-messaging.allow_gateway_injection: true``. Without a gateway route (CLI, sync
  one-shot runs) or when injection is refused, the text is steered into the live parent agent instead.
  ``settings.max_parent_messages_per_child`` (default 20) caps messages to the parent per child.

Seams (private core names; checked at ``register()``, one WARNING per unusable seam):
``tools.delegate_tool_registry`` (``_active_subagents``, ``_active_subagents_lock``, ``steer_subagent``),
``tools.registry.no_cache_check_fn`` + ``agent.delegation_context.is_delegated_child_context`` (children-only
tools), ``tools.tool_search.is_deferrable_tool_name`` (wrapped so these tools are never deferred behind the tool
search bridge), ``tools.async_delegation._records`` (the parent route when the parent agent object is gone).
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import logging
import threading
import time
from typing import Any, Optional

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

TOOLSET = "subagent-messaging"
_TOOL_NAMES = frozenset({"subagent_peers", "subagent_send", "subagent_wait"})
_PARENT_WINDOW_SECONDS = 2.0  # parent messages inside this window become one parent turn
_DEFAULT_MAX_PARENT_MESSAGES = 20
_MAX_MESSAGE_CHARS = 8000
_DEFAULT_WAIT_SECONDS = 300
_MAX_WAIT_SECONDS = 1800
_WAIT_POLL_SECONDS = 0.25
_WAIT_TOUCH_SECONDS = 15.0  # keep the activity clock moving while waiting (stall checks read it)
_GOAL_PREVIEW_CHARS = 160
_REGISTRY = "tools.delegate_tool_registry"
_TOOL_SEARCH = "tools.tool_search"
_WRAPPED_MARK = "_subagent_messaging_wrapped"

_registrations = 0
_parent_counts: dict[str, int] = {}  # sender subagent_id -> messages sent to its parent
_parent_counts_lock = threading.Lock()


# --- seams -------------------------------------------------------------------------------------------------------

def _params(fn: Any) -> Optional[list[str]]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return None


def _required_seam_problem() -> Optional[str]:
    """Why the subagent registry / children-only tool seams are unusable, or None."""
    try:
        registry = importlib.import_module(_REGISTRY)
        delegation_context = importlib.import_module("agent.delegation_context")
        tool_registry = importlib.import_module("tools.registry")
    except Exception as exc:
        return f"a core module cannot be imported ({type(exc).__name__}: {exc})"
    if not isinstance(getattr(registry, "_active_subagents", None), dict):
        return f"{_REGISTRY}._active_subagents is missing"
    if not hasattr(getattr(registry, "_active_subagents_lock", None), "__enter__"):
        return f"{_REGISTRY}._active_subagents_lock is missing"
    steer = getattr(registry, "steer_subagent", None)
    if not callable(steer) or (_params(steer) or [])[:2] != ["subagent_id", "text"]:
        return f"{_REGISTRY}.steer_subagent is missing or re-signed"
    if not callable(getattr(delegation_context, "is_delegated_child_context", None)):
        return "agent.delegation_context.is_delegated_child_context is missing"
    if not callable(getattr(tool_registry, "no_cache_check_fn", None)):
        return "tools.registry.no_cache_check_fn is missing"
    return None


def _child_only() -> bool:
    """check_fn: the tools exist only while a delegate_task child is being built or run."""
    from agent.delegation_context import is_delegated_child_context
    return is_delegated_child_context()


def _wrap_is_deferrable(original):
    @functools.wraps(original)
    def is_deferrable_tool_name(name, defer_tools=None):
        if name in _TOOL_NAMES and not (defer_tools is not None and name in defer_tools):
            return False  # a message tool behind tool_search is a tool the child never knows it has
        return original(name, defer_tools)

    setattr(is_deferrable_tool_name, _WRAPPED_MARK, original)
    return is_deferrable_tool_name


def _install_never_defer() -> Optional[str]:
    try:
        module = importlib.import_module(_TOOL_SEARCH)
    except Exception as exc:
        return f"{_TOOL_SEARCH} cannot be imported ({type(exc).__name__}: {exc})"
    current = getattr(module, "is_deferrable_tool_name", None)
    if getattr(current, _WRAPPED_MARK, None):
        return None
    if not callable(current) or _params(current) != ["name", "defer_tools"]:
        return f"{_TOOL_SEARCH}.is_deferrable_tool_name is missing or re-signed"
    module.is_deferrable_tool_name = _wrap_is_deferrable(current)
    return None


def _uninstall_never_defer() -> None:
    module = importlib.import_module(_TOOL_SEARCH)
    original = getattr(getattr(module, "is_deferrable_tool_name", None), _WRAPPED_MARK, None)
    if original is not None:
        module.is_deferrable_tool_name = original


# --- registry views ----------------------------------------------------------------------------------------------

def _registry():
    return importlib.import_module(_REGISTRY)


def _record(subagent_id: Any) -> Optional[dict]:
    """A copy of the live registry record (``agent`` included), or None."""
    if not isinstance(subagent_id, str) or not subagent_id:
        return None
    registry = _registry()
    with registry._active_subagents_lock:
        record = registry._active_subagents.get(subagent_id)
        return dict(record) if record else None


def _parent_agent(agent: Any) -> Any:
    ref = getattr(agent, "_delegate_parent_ref", None)
    return ref() if callable(ref) else None


def _same_family(mine: dict, other: dict) -> bool:
    """Children of the same parent conversation: same parent subagent (None for top-level children) and the same
    owning session id — which also groups children of separate delegate_task calls. Without an owning session id
    only the identical parent agent object counts."""
    if mine.get("parent_id") != other.get("parent_id"):
        return False
    owner = mine.get("owner_agent_session_id")
    if owner:
        return owner == other.get("owner_agent_session_id")
    parent = _parent_agent(mine.get("agent"))
    return parent is not None and parent is _parent_agent(other.get("agent"))


def _siblings(me: dict) -> list[dict]:
    registry = _registry()
    with registry._active_subagents_lock:
        records = [dict(r) for sid, r in registry._active_subagents.items() if sid != me["subagent_id"]]
    found = [r for r in records if r.get("agent") is not None and r.get("accepting_steer") and _same_family(me, r)]
    return sorted(found, key=lambda r: r.get("started_at") or 0)


def _preview(goal: Any) -> str:
    text = " ".join(str(goal or "").split())
    return text if len(text) <= _GOAL_PREVIEW_CHARS else text[: _GOAL_PREVIEW_CHARS - 1] + "…"


# --- message text ------------------------------------------------------------------------------------------------

def _sibling_text(sender: dict, message: str) -> str:
    sid = sender["subagent_id"]
    return (f"[SUBAGENT MESSAGE from sibling {sid} (task: {_preview(sender.get('goal'))})]\n{message}\n"
            f'[/SUBAGENT MESSAGE — answer with subagent_send(to="{sid}", message=...) if it needs a reply]')


def _parent_text(entries: list[tuple[dict, str]]) -> str:
    blocks = []
    for sender, message in entries:
        sid = sender["subagent_id"]
        blocks.append(f"From {sid} (task: {_preview(sender.get('goal'))}):\n{message}\n"
                      f'Reply if needed: delegate_task(action="steer", subagent_id="{sid}", message="...")')
    return ("[SUBAGENT MESSAGES — from delegate_task subagents that are still running; not from the user]\n"
            + "\n\n".join(blocks)
            + "\n[/SUBAGENT MESSAGES — each subagent's final result still arrives on its own]")


# --- parent delivery ---------------------------------------------------------------------------------------------

def _parent_route(agent: Any, parent: Any) -> Optional[str]:
    """The parent conversation's session key: the live parent agent's gateway key, else the route the background
    delegation's completion is delivered to."""
    key = getattr(parent, "_gateway_session_key", None) if parent is not None else None
    if isinstance(key, str) and key:
        return key
    delegation_id = getattr(agent, "_delegation_id", None)
    if not delegation_id:
        return None
    try:
        async_delegation = importlib.import_module("tools.async_delegation")
        with async_delegation._records_lock:
            for unit_id, record in async_delegation._records.items():
                if unit_id == delegation_id or unit_id.startswith(f"{delegation_id}-"):
                    key = record.get("session_key")
                    if isinstance(key, str) and key:
                        return key
    except Exception:
        logger.debug("subagent-messaging: async delegation route lookup failed", exc_info=True)
    return None


class _ParentOutbox:
    """Joins messages to one parent conversation that arrive within the window into one delivery."""

    def __init__(self):
        self._lock = threading.Lock()
        self._batches: dict[Any, dict] = {}

    def add(self, ctx: Any, route: Optional[str], parent: Any, sender: dict, message: str) -> None:
        key = route or id(parent)
        with self._lock:
            batch = self._batches.get(key)
            if batch is None:
                timer = threading.Timer(_PARENT_WINDOW_SECONDS, self._flush, args=(key,))
                timer.daemon = True
                batch = self._batches[key] = {"ctx": ctx, "route": route, "parent": parent, "entries": [],
                                              "timer": timer}
                timer.start()
            batch["entries"].append((sender, message))

    def _flush(self, key: Any) -> None:
        with self._lock:
            batch = self._batches.pop(key, None)
        if batch is not None:
            _deliver_parent_batch(batch)

    def flush_all(self) -> None:
        with self._lock:
            keys = list(self._batches)
            for key in keys:
                self._batches[key]["timer"].cancel()
        for key in keys:
            self._flush(key)


_OUTBOX = _ParentOutbox()


def _deliver_parent_batch(batch: dict) -> None:
    text = _parent_text(batch["entries"])
    senders = ", ".join(sender["subagent_id"] for sender, _ in batch["entries"])
    route, parent = batch["route"], batch["parent"]
    if route:
        try:
            if batch["ctx"].inject_message(text, session_key=route):
                logger.info("subagent-messaging: message(s) from %s queued as a turn of %s", senders, route)
                return
        except Exception:
            logger.warning("subagent-messaging: inject_message into %s failed", route, exc_info=True)
    steer = getattr(parent, "steer", None)
    if callable(steer):
        try:
            if steer(text):
                logger.info("subagent-messaging: message(s) from %s steered into the parent agent", senders)
                return
        except Exception:
            logger.debug("subagent-messaging: parent steer failed", exc_info=True)
    logger.warning("subagent-messaging: message(s) from %s to their parent were not delivered (no gateway route "
                   "accepted them and the parent agent is gone)", senders)


def _max_parent_messages(ctx: Any) -> int:
    try:
        value = int(ctx.get_config("max_parent_messages_per_child", _DEFAULT_MAX_PARENT_MESSAGES))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_PARENT_MESSAGES
    return max(1, value)


def _take_parent_slot(sender_id: str, cap: int) -> bool:
    with _parent_counts_lock:
        count = _parent_counts.get(sender_id, 0)
        if count >= cap:
            return False
        _parent_counts[sender_id] = count + 1
        while len(_parent_counts) > 1000:
            _parent_counts.pop(next(iter(_parent_counts)))
        return True


def _send_to_parent(ctx: Any, me: dict, message: str) -> dict:
    cap = _max_parent_messages(ctx)
    if not _take_parent_slot(me["subagent_id"], cap):
        return {"error": f"You already sent {cap} messages to your parent; put anything else in your final result."}
    parent_id = me.get("parent_id")
    if parent_id:  # the parent is itself a subagent (orchestrator) running in this process
        if _registry().steer_subagent(parent_id, _parent_text([(me, message)])):
            return {"delivered": ["parent"], "note": "Queued into your parent's running turn."}
        return {"error": f"Your parent {parent_id} is not running any more; put this in your final result."}
    agent = me["agent"]
    parent = _parent_agent(agent)
    route = _parent_route(agent, parent)
    if route is None and parent is None:
        return {"error": "Your parent conversation is gone; put this in your final result."}
    _OUTBOX.add(ctx, route, parent, me, message)
    return {"delivered": ["parent"],
            "note": "Your parent gets it as a new message within a few seconds; keep working."}


# --- tools -------------------------------------------------------------------------------------------------------

_NOT_A_SUBAGENT = "Only a running delegate_task subagent can use {tool}."


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _caller(task_id: Any, tool: str) -> tuple[Optional[dict], Optional[str]]:
    me = _record(task_id)
    if me is None or me.get("agent") is None:
        return None, _json({"error": _NOT_A_SUBAGENT.format(tool=tool)})
    return me, None


def _handle_peers(args: dict, task_id: Any = None, **_kwargs: Any) -> str:
    me, error = _caller(task_id, "subagent_peers")
    if error:
        return error
    now = time.time()
    siblings = [{
        "subagent_id": r["subagent_id"], "goal": _preview(r.get("goal")), "status": r.get("status"),
        "tool_count": r.get("tool_count"), "last_tool": r.get("last_tool"),
        "running_seconds": round(now - r["started_at"]) if r.get("started_at") else None,
    } for r in _siblings(me)]
    you = {"subagent_id": me["subagent_id"], "goal": _preview(me.get("goal")),
           "parent": me.get("parent_id") or "the conversation that delegated you"}
    return _json({"you": you, "siblings": siblings})


def _make_send_handler(ctx: Any):
    def _handle_send(args: dict, task_id: Any = None, **_kwargs: Any) -> str:
        me, error = _caller(task_id, "subagent_send")
        if error:
            return error
        to = str(args.get("to") or "").strip()
        message = str(args.get("message") or "").strip()
        if not to or not message:
            return _json({"error": "Both 'to' and 'message' are required."})
        if len(message) > _MAX_MESSAGE_CHARS:
            return _json({"error": f"Message too long ({len(message)} chars, max {_MAX_MESSAGE_CHARS})."})
        if to == "parent":
            return _json(_send_to_parent(ctx, me, message))
        steer = _registry().steer_subagent
        if to == "siblings":
            delivered, missed = [], []
            for sibling in _siblings(me):
                ok = steer(sibling["subagent_id"], _sibling_text(me, message))
                (delivered if ok else missed).append(sibling["subagent_id"])
            result: dict = {"delivered": delivered}
            if missed:
                result["not_running"] = missed
            if not delivered:
                result["note"] = "No sibling is running."
            return _json(result)
        if to == me["subagent_id"]:
            return _json({"error": "That is your own subagent_id."})
        target = _record(to)
        if target is not None and not _same_family(me, target):
            return _json({"error": f"{to} is not one of your siblings (see subagent_peers)."})
        if target is None or not steer(to, _sibling_text(me, message)):
            return _json({"error": f"{to} is not running (finished or unknown); put what it needs in your "
                                   "final result instead."})
        return _json({"delivered": [to], "note": "It arrives in that subagent's conversation after its current step."})

    return _handle_send


def _steer_pending(agent: Any) -> bool:
    lock = getattr(agent, "_pending_steer_lock", None)
    if lock is None:
        return bool(getattr(agent, "_pending_steer", None))
    with lock:
        return bool(getattr(agent, "_pending_steer", None))


def _interrupted(agent: Any) -> bool:
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return True
    except Exception:
        pass
    return bool(getattr(agent, "_interrupt_requested", False))


def _handle_wait(args: dict, task_id: Any = None, **_kwargs: Any) -> str:
    me, error = _caller(task_id, "subagent_wait")
    if error:
        return error
    agent = me["agent"]
    try:
        seconds = int(args.get("timeout_seconds") or _DEFAULT_WAIT_SECONDS)
    except (TypeError, ValueError):
        seconds = _DEFAULT_WAIT_SECONDS
    seconds = min(max(seconds, 1), _MAX_WAIT_SECONDS)
    deadline = time.monotonic() + seconds
    next_touch = 0.0
    while True:
        if _steer_pending(agent):
            return _json({"status": "message_pending", "note": "The message follows right after this result."})
        if _interrupted(agent):
            return _json({"status": "interrupted"})
        now = time.monotonic()
        if now >= next_touch:
            touch = getattr(agent, "_touch_activity", None)
            if callable(touch):
                try:
                    touch("subagent_wait: waiting for a message")
                except Exception:
                    logger.debug("subagent-messaging: activity touch failed", exc_info=True)
            next_touch = now + _WAIT_TOUCH_SECONDS
        if now >= deadline:
            return _json({"status": "timeout", "waited_seconds": seconds, "note": "No message arrived."})
        time.sleep(min(_WAIT_POLL_SECONDS, deadline - now))


def _schemas(max_parent: int) -> dict[str, dict]:
    def schema(name, description, properties=None, required=None):
        params: dict = {"type": "object", "properties": properties or {}, "additionalProperties": False}
        if required:
            params["required"] = required
        return {"name": name, "description": description, "parameters": params}

    return {
        "subagent_peers": schema(
            "subagent_peers",
            "Show your own subagent_id and the other subagents your parent is running right now (your siblings): "
            "id, task, status and last tool. Use the ids with subagent_send."),
        "subagent_send": schema(
            "subagent_send",
            "Send a message while you keep working (you do not finish). to='parent': your parent gets it as a "
            "new message in its conversation — use it for progress it needs now, a question, or a blocker "
            f"(at most {max_parent} per task; your final result still reaches it as usual). to=<sibling "
            "subagent_id> or 'siblings': the text is inserted into that sibling's conversation after its current "
            "step. Your parent and siblings reach you the same way: their text appears as an out-of-band "
            "message, sibling ones starting with [SUBAGENT MESSAGE from sibling <id>].",
            {"to": {"type": "string",
                    "description": "'parent', a sibling's subagent_id (see subagent_peers), or 'siblings' for every "
                                   "running sibling."},
             "message": {"type": "string", "description": "The message text."}},
            ["to", "message"]),
        "subagent_wait": schema(
            "subagent_wait",
            "Pause until a message from your parent or a sibling arrives, or the timeout passes. Returns "
            "status 'message_pending' (the message follows right after this result) or 'timeout'. Use it when "
            "you cannot continue without an answer.",
            {"timeout_seconds": {"type": "integer", "minimum": 1, "maximum": _MAX_WAIT_SECONDS,
                                 "description": f"How long to wait (default {_DEFAULT_WAIT_SECONDS})."}}),
    }


# --- lifecycle ---------------------------------------------------------------------------------------------------

def _on_unload() -> None:
    global _registrations
    _registrations = max(0, _registrations - 1)
    _OUTBOX.flush_all()
    if not _registrations:
        _uninstall_never_defer()


def register(ctx: Any) -> None:
    global _registrations
    problem = _required_seam_problem()
    if problem is not None:
        logger.warning("subagent-messaging: %s, so subagents get no messaging tools. Update the plugin for this "
                       "Hermes version.", problem)
        return
    from tools.registry import no_cache_check_fn
    no_cache_check_fn(_child_only)
    handlers = {"subagent_peers": _handle_peers, "subagent_send": _make_send_handler(ctx),
                "subagent_wait": _handle_wait}
    emoji = {"subagent_peers": "👥", "subagent_send": "📨", "subagent_wait": "⏳"}
    for name, schema in _schemas(_max_parent_messages(ctx)).items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handlers[name], check_fn=_child_only,
                          description=schema["description"], emoji=emoji[name])
    _registrations += 1
    ctx.on_unload(_on_unload)
    defer_problem = _install_never_defer()
    if defer_problem is not None:
        logger.warning("subagent-messaging: %s, so with tool_search on the messaging tools may sit behind "
                       "tool_search. Update the plugin for this Hermes version.", defer_problem)
    try:
        async_delegation = importlib.import_module("tools.async_delegation")
        route_ok = isinstance(getattr(async_delegation, "_records", None), dict) and hasattr(
            getattr(async_delegation, "_records_lock", None), "__enter__")
    except Exception:
        route_ok = False
    if not route_ok:
        logger.warning("subagent-messaging: tools.async_delegation._records is missing, so a child whose parent "
                       "agent was rebuilt can only reach the parent while it is in memory. Update the plugin for "
                       "this Hermes version.")
    logger.info("subagent-messaging v%s armed", __version__)


__all__ = ["register", "__version__"]
