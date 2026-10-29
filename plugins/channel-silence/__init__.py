"""Hermes channel-silence plugin: per-channel ``allow_silence``.

``<platform>.channel_overrides.<id>.allow_silence: true`` (e.g. ``discord.channel_overrides``; the
nested ``platforms.<platform>`` / ``gateway.platforms.<platform>`` spellings work too) lets a human
turn in that channel — or in a thread under it, via the gateway's chat → thread → parent lookup — end
on a bare silence marker (``[SILENT]``/``NO_REPLY``) without the visible ⚠️ fallback notice.

Hermes already decides silence with ``silence_allowed(display_kind, reply_expected)``; this plugin
only stamps ``MessageEvent.reply_expected = False`` on every non-internal event from an opted-in
channel, so the core's unaddressed-silence path applies unchanged: no ⚠️ fallback, merged bursts
(``absorb_reply_expected``) and a queued chain's terminal turn carry the verdict, and the persisted
user row keeps ``reply_expected: false`` for crash-recovery replay. Internal turns keep their own
verdict.

Where the stamp happens
-----------------------
1. ``BasePlatformAdapter._drop_unresolved`` — the identity gate every adapter ingress runs first
   (``handle_message``, the text-batch ``_enqueue_text_event``, Telegram's batch lane) — is wrapped
   so a resolved event is stamped right there, BEFORE session-key derivation, busy queueing, text
   debounce or burst merging. Without this a follow-up arriving mid-turn would land in the pending
   slot / runner FIFO / steer fold unstamped (``pre_gateway_dispatch`` never sees busy follow-ups:
   the queued chain drains them inside the running turn), and its silent reply would get the ⚠️.
2. ``pre_gateway_dispatch`` (the official hook) stamps the event too: it covers events that reach
   the runner without an adapter ingress (producer paths) and is the fallback if the wrapper could
   not be installed (logged).

The config is read from the profile's ``config.yaml`` itself: core ``ChannelOverride.from_dict``
keeps only the keys it knows, so ``allow_silence`` never reaches ``PlatformConfig``.
"""

from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path
from typing import Any, Optional

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

_BASE_MODULE = "gateway.platforms.base"
_WRAPPED_MARK = "_channel_silence_wrapped"
_registrations = 0  # live register() calls (one per profile loading the plugin in this process)


def _overrides_for(config: Any, platform_key: str) -> dict:
    """``channel_overrides`` of *platform_key*'s config section, using the gateway's own section
    precedence (top-level ``<platform>:`` block, else ``gateway.platforms`` / ``platforms``)."""
    if not isinstance(config, dict):
        return {}
    from gateway.config_loader import platform_section
    gateway_section = config.get("gateway")
    gateway_platforms = gateway_section.get("platforms") if isinstance(gateway_section, dict) else None
    section, _toplevel = platform_section(config, platform_key, gateway_platforms)
    overrides = section.get("channel_overrides") if isinstance(section, dict) else None
    return overrides if isinstance(overrides, dict) else {}


def _load_profile_config(profile: Optional[str]) -> Any:
    """The profile's user config.yaml as the gateway reads it (``gateway.run._load_gateway_config``:
    ``${VAR}`` expansion + managed overlay, NO ``DEFAULT_CONFIG`` merge — a defaults-only top-level
    ``<platform>:`` block would otherwise shadow a nested ``platforms.<platform>`` one). A routed
    multiplex profile reads its own home."""
    from hermes_cli.config_effective import load_user_config_effective
    if profile:
        from hermes_cli.profiles import get_profile_dir
        home = get_profile_dir(profile)
    else:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return load_user_config_effective(Path(home) / "config.yaml")


def channel_allows_silence(source: Any, profile: Optional[str] = None) -> bool:
    """True when *source*'s channel override (first of chat → thread → parent that exists, as in the
    gateway's ``channel_overrides`` lookup) sets ``allow_silence``. Never raises."""
    try:
        platform = getattr(source, "platform", None)
        platform_key = getattr(platform, "value", None)
        if not platform_key:
            return False
        overrides = _overrides_for(_load_profile_config(profile), str(platform_key))
        if not overrides:
            return False
        keys = dict.fromkeys(
            str(key) for key in (
                getattr(source, "chat_id", None), getattr(source, "thread_id", None),
                getattr(source, "parent_chat_id", None))
            if key)
        override = next(
            (overrides[key] for key in keys if isinstance(overrides.get(key), dict)), None)
        if override is None:
            return False
        from gateway.config import _coerce_bool
        return _coerce_bool(override.get("allow_silence"), False)
    except Exception:
        logger.debug("channel-silence: allow_silence lookup failed", exc_info=True)
        return False


def apply_channel_silence(event: Any, profile: Optional[str] = None) -> bool:
    """Stamp a non-internal *event* from an opted-in channel ``reply_expected=False``; True if so."""
    if event is None or getattr(event, "internal", False):
        return False
    source = getattr(event, "source", None)
    if source is None or not channel_allows_silence(source, profile):
        return False
    event.reply_expected = False
    return True


def _adapter_profile(adapter: Any, source: Any) -> Optional[str]:
    """The profile whose config governs *adapter*'s inbound (the adapter's session-key profile)."""
    resolver = getattr(adapter, "_session_key_profile", None)
    if callable(resolver):
        try:
            profile = resolver(source)
        except Exception:
            profile = None
        if isinstance(profile, str) and profile.strip():
            return profile
    profile = getattr(source, "profile", None)
    return profile if isinstance(profile, str) and profile.strip() else None


def _wrap_drop_unresolved(original):
    @functools.wraps(original)
    def _drop_unresolved(self, event):
        dropped = original(self, event)
        if not dropped:
            try:
                apply_channel_silence(event, _adapter_profile(self, getattr(event, "source", None)))
            except Exception:
                logger.debug("channel-silence: adapter ingress stamp failed", exc_info=True)
        return dropped

    setattr(_drop_unresolved, _WRAPPED_MARK, original)
    return _drop_unresolved


def _install_adapter_ingress() -> bool:
    """Wrap ``BasePlatformAdapter._drop_unresolved`` once (idempotent); False when the gateway
    adapter base is not loaded in this process or lacks the gate."""
    module = sys.modules.get(_BASE_MODULE)
    base = getattr(module, "BasePlatformAdapter", None) if module is not None else None
    current = getattr(base, "_drop_unresolved", None) if base is not None else None
    if current is None:
        return False
    if not getattr(current, _WRAPPED_MARK, None):
        base._drop_unresolved = _wrap_drop_unresolved(current)
    return True


def _uninstall_adapter_ingress() -> None:
    module = sys.modules.get(_BASE_MODULE)
    base = getattr(module, "BasePlatformAdapter", None) if module is not None else None
    current = getattr(base, "_drop_unresolved", None) if base is not None else None
    original = getattr(current, _WRAPPED_MARK, None)
    if original is not None:
        base._drop_unresolved = original


async def _pre_gateway_dispatch(event: Any = None, gateway: Any = None, session_store: Any = None,
                                **_kwargs: Any) -> None:
    # The adapter base is loaded by now in a gateway process: install the ingress stamp if the
    # plugin registered before it was imported (a few getattrs once installed).
    if _registrations:
        _install_adapter_ingress()
    source = getattr(event, "source", None)
    apply_channel_silence(event, getattr(source, "profile", None))
    return None


def _on_unload() -> None:
    global _registrations
    _registrations = max(0, _registrations - 1)
    if not _registrations:
        _uninstall_adapter_ingress()


def register(ctx: Any) -> None:
    global _registrations
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    _registrations += 1
    ctx.on_unload(_on_unload)
    if not _install_adapter_ingress():
        logger.info(
            "channel-silence: %s not loaded yet; adapter ingress stamp deferred to the first "
            "pre_gateway_dispatch", _BASE_MODULE)
    logger.info("channel-silence v%s armed", __version__)


__all__ = ["register", "apply_channel_silence", "channel_allows_silence", "__version__"]
