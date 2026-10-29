"""skills.always_loaded: put the listed skills' SKILL.md into EVERY agent's system prompt.

Every agent — CLI, gateway, TUI, cron, delegate_task children, background-review forks — sends its
model calls through the agent loop's ``llm_request`` middleware seam, and that is the only plugin
seam that reaches all of them with room for full skill payloads (plugin system-prompt sections are
capped at 4,000 chars each / 8,000 in total, and no hook hands a plugin the agent object). So the
blocks are appended to the request's system message at API time, like ``ephemeral_system_prompt``:

- Rendered once per (profile home, session, name set) with Hermes' own preloaded-skill renderer
  (``agent.skill_commands.build_preloaded_skills_prompt``), so every call of a session sends the same
  bytes (prompt cache stays warm, ``!`cmd``` snippets are not re-run per call).
- Skills already carried by the system text as a preloaded block (``hermes -s``, a core
  ``skills.always_loaded``, a fork that inherited its parent's ephemeral prompt) or an auto-loaded
  block (``skills.auto_load``) are not rendered again.
- Missing/disabled names are skipped; one warning per profile home and name set. Nothing here may
  fail a model call: any error leaves the request untouched.

Config is read from the profile config the running agent uses (``load_config_readonly``):
``skills.always_loaded`` as a list or a comma/newline-separated string.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Activation notes of the blocks Hermes renders for preloaded (``build_preloaded_skills_prompt``) and
# auto-loaded (``build_auto_load_prompt``) skills; their names count as already loaded.
_LOADED_SKILL_MARKERS = (
    re.compile(r'with the "([^"]+)" skill\s+preloaded'),
    re.compile(r'The "([^"]+)" skill is auto-loaded via config'),
)
_RENDER_CACHE_MAX = 512

_lock = threading.Lock()
_render_cache: "OrderedDict[tuple, str]" = OrderedDict()
_warned_missing: set[tuple[str, str]] = set()


def resolve_always_loaded_skills(user_config: Any) -> list[str]:
    """``skills.always_loaded`` from *user_config*, deduplicated; a list or a comma/newline-separated
    string. Empty when unset or malformed."""
    skills_block = user_config.get("skills") if isinstance(user_config, dict) else None
    raw = skills_block.get("always_loaded") if isinstance(skills_block, dict) else None
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    names = [str(entry).strip() for entry in raw if isinstance(entry, (str, int)) and str(entry).strip()]
    return list(dict.fromkeys(names))


def _configured_names() -> list[str]:
    try:
        from hermes_cli.config import load_config_readonly
        return resolve_always_loaded_skills(load_config_readonly())
    except Exception:
        logger.debug("skills.always_loaded: config unreadable", exc_info=True)
        return []


def _home_key() -> str:
    from hermes_constants import get_hermes_home, hermes_home_key
    return hermes_home_key(get_hermes_home())


# --- the request's system text, per wire shape -------------------------------------------------
# codex_responses: ``instructions`` (str); anthropic_messages: ``system`` (str | [{"type": "text",
# "text", "cache_control"?}]); bedrock_converse: ``system`` ([{"text"} | {"cachePoint"}]);
# chat_completions and chat-shaped plugin transports: leading ``messages`` with role system/developer
# (content str | [{"type": "text", "text"}]).

def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [part["text"] for part in value if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return []


def _system_text(request: dict) -> str:
    parts = _texts(request.get("instructions")) + _texts(request.get("system"))
    for message in request.get("messages") or ():
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            break
        parts += _texts(message.get("content"))
    return "\n\n".join(parts)


def _append_to(value: Any, text: str) -> Any:
    """*value* (a str or a list of text parts) with *text* appended to its LAST text part, so cache
    markers that follow it (Bedrock ``cachePoint``) or ride on it (``cache_control``) keep covering
    it. ``None`` when *value* has no text to extend."""
    if isinstance(value, str):
        return f"{value}\n\n{text}" if value else text
    if isinstance(value, list):
        for index in range(len(value) - 1, -1, -1):
            part = value[index]
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                value[index] = {**part, "text": f"{part['text']}\n\n{text}" if part["text"] else text}
                return value
    return None


def inject_system_text(request: dict, text: str, api_mode: str = "") -> bool:
    """Append *text* to the system message of *request* in place; False when the shape is unknown."""
    for key in ("instructions", "system"):
        if key in request and request[key]:
            extended = _append_to(request[key], text)
            if extended is not None:
                request[key] = extended
                return True
    if api_mode == "anthropic_messages":
        request["system"] = text
        return True
    if api_mode == "bedrock_converse":
        request["system"] = [{"text": text}]
        return True
    if api_mode == "codex_responses" or ("input" in request and "messages" not in request):
        request["instructions"] = text
        return True
    messages = request.get("messages")
    if isinstance(messages, list):
        first = messages[0] if messages else None
        if isinstance(first, dict) and first.get("role") in ("system", "developer"):
            extended = _append_to(first.get("content"), text)
            if extended is not None:
                messages[0] = {**first, "content": extended}
                return True
        messages.insert(0, {"role": "system", "content": text})
        return True
    return False


# --- rendering ----------------------------------------------------------------------------------

def _render(names: list[str], session_id: str, already_loaded: frozenset[str]) -> str:
    """Rendered blocks for *names* minus *already_loaded*, cached per home/session/name set."""
    home = _home_key()
    key = (home, session_id, tuple(names), already_loaded)
    with _lock:
        if key in _render_cache:
            _render_cache.move_to_end(key)
            return _render_cache[key]
    from agent.skill_commands import build_preloaded_skills_prompt

    prompt, _loaded, missing = build_preloaded_skills_prompt(
        names, task_id=session_id or None, excluded_loaded_names=set(already_loaded))
    with _lock:
        if missing and (warn_key := (home, ", ".join(missing))) not in _warned_missing:
            _warned_missing.add(warn_key)
            logger.warning("skills.always_loaded: skill(s) not found or disabled, skipped: %s", warn_key[1])
        _render_cache[key] = prompt
        while len(_render_cache) > _RENDER_CACHE_MAX:
            _render_cache.popitem(last=False)
    return prompt


def always_loaded_middleware(request: Optional[dict] = None, session_id: str = "", api_mode: str = "",
                             **_context: Any) -> Optional[dict]:
    """``llm_request`` middleware: return ``{"request": ...}`` with the always-loaded skill blocks
    appended to the system message, or None (request untouched)."""
    if not isinstance(request, dict):
        return None
    try:
        names = _configured_names()
        if not names:
            return None
        system = _system_text(request)
        already = frozenset(name for marker in _LOADED_SKILL_MARKERS for name in marker.findall(system))
        prompt = _render(names, str(session_id or ""), already)
        if not prompt or not inject_system_text(request, prompt, str(api_mode or "")):
            return None
        return {"request": request, "source": "always-loaded-skills", "reason": "skills.always_loaded"}
    except Exception:
        # A malformed optional skill must never break a model call.
        logger.debug("skills.always_loaded: injection skipped", exc_info=True)
        return None


def register(ctx: Any) -> None:
    ctx.register_middleware("llm_request", always_loaded_middleware)
