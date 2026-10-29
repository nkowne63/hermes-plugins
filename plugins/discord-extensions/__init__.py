"""discord-extensions: the bundled Discord adapter plus two fork features, as a user platform plugin.

``register()`` runs the bundled Discord plugin's own ``register()`` through a context proxy that swaps
``adapter_factory`` for a ``DiscordAdapter`` subclass. A user plugin loads after the bundled platform's
deferred loader is armed, so this registration supersedes it; unloading the plugin restores it (the
plugin ledger's inverse). The subclass adds:

* ``discord.{channel,category,guild}_defaults`` (fork commit 57d2d5b2c2): per-scope ``require_mention`` /
  ``thread_response``, resolved channel (then a thread's parent) > category (the channel's, or the thread
  parent's) > guild > global flag, and ``no_thread_channels`` matching a category ID. Implemented as
  ContextVar-scoped overrides of three private helpers, active only while the message they concern is in
  one of the three ingress methods that call them.
* Auto-thread starter reply references (fork commit e8e420baae): a reply into an auto-thread whose target is
  the thread starter names the parent channel, and only reference-shaped 50035s / 10008 retry as a plain send.

Every private name the subclass relies on is checked at register time (see ``_DEPENDENCIES``); a feature
whose dependency is gone or re-signed is switched off with one WARNING naming what is lost.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import logging
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

PLATFORM_NAME = "discord"
SCOPED_DEFAULTS = "scoped_defaults"
STARTER_REPLY_REFERENCE = "starter_reply_reference"

FEATURE_DESCRIPTIONS = {
    SCOPED_DEFAULTS: "discord.{channel,category,guild}_defaults (require_mention / thread_response) and "
                     "category IDs in no_thread_channels",
    STARTER_REPLY_REFERENCE: "auto-thread starter replies via the parent channel and the targeted "
                             "reply-reference retry",
}

# (attribute on DiscordAdapter, parameter names after ``self`` / for a staticmethod, kind, feature).
# kind: "method" (plain function), "static" (staticmethod), "any" (either: upstream has a staticmethod the
# fork turned into a method). Every call into these goes through ``self.``, which is what lets a subclass
# override reach the bundled code paths.
_DEPENDENCIES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    # Ingress methods the scope is set around; the gates inside them call the three helpers below.
    ("_discord_message_admission", ("message", "claim"), "method", SCOPED_DEFAULTS),
    ("_dispatch_recovered_message", ("message",), "method", SCOPED_DEFAULTS),
    ("_handle_message", ("message", "role_authorized", "recovered"), "method", SCOPED_DEFAULTS),
    # Helpers overridden while a scope is active.
    ("_discord_require_mention", (), "method", SCOPED_DEFAULTS),
    ("_discord_free_response_channels", (), "method", SCOPED_DEFAULTS),
    ("_get_no_thread_channels", (), "method", SCOPED_DEFAULTS),
    # Called.
    ("_get_parent_channel_id", ("channel",), "method", SCOPED_DEFAULTS),
    # Reply references: every ids-built reference (send, overflow continuation, send_voice) comes from
    # _message_reference_from_ids, the send paths reach it through _reply_reference_for_send.
    ("_message_reference_from_ids", ("message_id", "channel"), "any", STARTER_REPLY_REFERENCE),
    ("_reply_reference_for_send", ("reply_to", "channel"), "method", STARTER_REPLY_REFERENCE),
    ("_is_reply_reference_rejected", ("err",), "static", STARTER_REPLY_REFERENCE),
    ("_is_forum_parent", ("channel",), "method", STARTER_REPLY_REFERENCE),
)

# Most specific scope first; a thread inherits its parent channel's rule (and, through the parent, the
# category's), so channel_defaults is matched against the thread then the parent.
_DEFAULT_SCOPES = ("channel_defaults", "category_defaults", "guild_defaults")

# Ingress stage of the message being gated, or None outside the three ingress methods.
#   admission: _discord_message_admission  ("mentions someone else" drop: scoped require_mention: false
#              counts as free-response)
#   recovered: _dispatch_recovered_message (recovered/missed-message mention gate)
#   handle:    _handle_message             (live mention gate and auto-thread decision)
_SCOPE: contextvars.ContextVar[Optional[tuple[Any, str]]] = contextvars.ContextVar(
    "discord_extensions_scope", default=None)


@contextlib.contextmanager
def _scoped(message: Any, stage: str) -> Iterator[None]:
    token = _SCOPE.set((message, stage))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _current(stage: str) -> Any:
    """The message under gate when the current stage is ``stage``, else None."""
    scope = _SCOPE.get()
    if scope is None or scope[1] != stage:
        return None
    return scope[0]


def check_dependencies(base: type) -> tuple[set[str], list[str]]:
    """Return ``(enabled features, problems)`` for the private names ``base`` must provide."""
    problems: dict[str, list[str]] = {}
    for name, params, kind, feature in _DEPENDENCIES:
        problem = _dependency_problem(base, name, params, kind)
        if problem:
            problems.setdefault(feature, []).append(problem)
    enabled = {feature for feature in FEATURE_DESCRIPTIONS if feature not in problems}
    return enabled, [problem for feature in FEATURE_DESCRIPTIONS for problem in problems.get(feature, [])]


def _dependency_problem(base: type, name: str, params: tuple[str, ...], kind: str) -> Optional[str]:
    try:
        raw = inspect.getattr_static(base, name)
    except AttributeError:
        return f"{base.__name__}.{name} is missing"
    is_static = isinstance(raw, staticmethod)
    func = raw.__func__ if is_static else raw
    if not inspect.isfunction(func):
        return f"{base.__name__}.{name} is not a function"
    if kind == "static" and not is_static:
        return f"{base.__name__}.{name} is no longer a staticmethod"
    if kind == "method" and is_static:
        return f"{base.__name__}.{name} became a staticmethod"
    actual = tuple(inspect.signature(func).parameters)
    if not is_static:
        actual = actual[1:]
    if actual != params:
        return f"{base.__name__}.{name}{actual} != expected {params}"
    return None


def build_adapter_class(adapter_module: Any, features: set[str]) -> type:
    """``DiscordAdapter`` subclass with the given features on. ``discord`` is read from ``adapter_module``
    at call time (tests swap ``discord.MessageReference``, as for the bundled class)."""
    base = adapter_module.DiscordAdapter
    scoped_defaults = SCOPED_DEFAULTS in features
    starter_reply_reference = STARTER_REPLY_REFERENCE in features

    class DiscordExtensionsAdapter(base):
        discord_extensions_features = frozenset(features)

        # ── scoped defaults: ingress scopes ──────────────────────────────────────────────────────

        def _discord_message_admission(self, message: Any, *args: Any, **kwargs: Any):
            if not scoped_defaults:
                return super()._discord_message_admission(message, *args, **kwargs)
            with _scoped(message, "admission"):
                return super()._discord_message_admission(message, *args, **kwargs)

        async def _dispatch_recovered_message(self, message: Any, *args: Any, **kwargs: Any):
            if not scoped_defaults:
                return await super()._dispatch_recovered_message(message, *args, **kwargs)
            with _scoped(message, "recovered"):
                return await super()._dispatch_recovered_message(message, *args, **kwargs)

        async def _handle_message(self, message: Any, *args: Any, **kwargs: Any):
            if not scoped_defaults:
                return await super()._handle_message(message, *args, **kwargs)
            with _scoped(message, "handle"):
                return await super()._handle_message(message, *args, **kwargs)

        # ── scoped defaults: helpers the gates call ──────────────────────────────────────────────

        def _discord_require_mention(self) -> bool:
            """Live and recovered mention gates: the scoped rule beats the global flag."""
            global_flag = super()._discord_require_mention()
            scope = _SCOPE.get()
            if not scoped_defaults or scope is None or scope[1] not in {"handle", "recovered"}:
                return global_flag
            scoped = self._discord_extensions_scoped_flag(scope[0], "require_mention")
            return global_flag if scoped is None else scoped

        def _discord_free_response_channels(self) -> set:
            """Admission only: a scoped ``require_mention: false`` marks the channel free-response for the
            "mentions someone else" drop. Mention gates, thread decisions and backfill see the plain set."""
            channels = super()._discord_free_response_channels()
            message = _current("admission") if scoped_defaults else None
            if message is None or self._discord_extensions_scoped_flag(message, "require_mention") is not False:
                return channels
            channel_id = getattr(getattr(message, "channel", None), "id", None)
            return channels if channel_id is None else set(channels) | {str(channel_id)}

        def _get_no_thread_channels(self) -> set:
            """Auto-thread decision: the channel also counts as no-thread when its category is listed or a
            scoped ``thread_response: false`` applies. A scoped ``true`` never removes an exclusion."""
            channels = super()._get_no_thread_channels()
            message = _current("handle") if scoped_defaults else None
            if message is None:
                return channels
            channel = getattr(message, "channel", None)
            channel_id = getattr(channel, "id", None)
            if channel_id is None:
                return channels
            category_id = self._discord_extensions_category_id(channel)
            if (category_id is not None and category_id in channels) or (
                self._discord_extensions_scoped_flag(message, "thread_response") is False
            ):
                return set(channels) | {str(channel_id)}
            return channels

        def _discord_extensions_scoped_flag(self, message: Any, key: str) -> Optional[bool]:
            """``key`` from channel_defaults > category_defaults > guild_defaults; None = no rule.

            Each section maps an ID to a dict of overrides (``require_mention``, ``thread_response``).
            IDs are compared as strings, so unquoted YAML integer keys work too."""
            extra = getattr(self.config, "extra", None)
            if not isinstance(extra, dict):
                return None
            channel = getattr(message, "channel", None)
            guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
            scope_ids = {
                "channel_defaults": (getattr(channel, "id", None), self._get_parent_channel_id(channel)),
                "category_defaults": (self._discord_extensions_category_id(channel),),
                "guild_defaults": (getattr(guild, "id", None),),
            }
            for section_name in _DEFAULT_SCOPES:
                section = extra.get(section_name)
                if not isinstance(section, dict):
                    continue
                rules = {str(rule_id).strip(): rule for rule_id, rule in section.items()}
                for raw_id in scope_ids[section_name]:
                    if raw_id is None:
                        continue
                    rule = rules.get(str(raw_id))
                    if isinstance(rule, dict) and rule.get(key) is not None:
                        value = rule[key]
                        if isinstance(value, bool):
                            return value
                        return str(value).strip().lower() in {"true", "1", "yes", "on"}
            return None

        @staticmethod
        def _discord_extensions_category_id(channel: Any) -> Optional[str]:
            """Category ID of a channel, or of a thread's parent channel, if known."""
            for candidate in (channel, getattr(channel, "parent", None)):
                if candidate is None:
                    continue
                category_id = getattr(candidate, "category_id", None)
                if category_id is None:
                    category_id = getattr(getattr(candidate, "category", None), "id", None)
                if category_id is not None:
                    return str(category_id)
            return None

        # ── auto-thread starter reply references ─────────────────────────────────────────────────

        def _discord_extensions_reply_channel_id(self, message_id: Any, channel: Any) -> Any:
            """Channel that holds ``message_id`` for a reply sent into ``channel``: the send target, except
            a thread started from a text-channel message. Discord gives that thread the starter message's
            id, but the message lives in the parent channel (inside the thread it is only a
            THREAD_STARTER_MESSAGE system stub that cannot be replied to), so the reference names the
            parent. Forum/media posts keep their real starter inside the thread, and an uncached parent
            stays on the thread; both degrade to the plain-send retry if rejected."""
            channel_id = getattr(channel, "id", None)
            if channel_id is None or str(channel_id) != str(message_id):
                return channel_id
            parent = getattr(channel, "parent", None)
            parent_id = getattr(channel, "parent_id", None)
            if parent is None or parent_id is None or self._is_forum_parent(parent):
                return channel_id
            return parent_id

        def _message_reference_from_ids(self, message_id: Any, channel: Any) -> Any:
            """ids-built reply reference, no fetch_message round trip. fail_if_not_exists=False keeps
            sends to deleted targets degrading to the send-side 10008 retry."""
            if not starter_reply_reference:
                return super()._message_reference_from_ids(message_id, channel)
            return adapter_module.discord.MessageReference(
                message_id=int(message_id),
                channel_id=self._discord_extensions_reply_channel_id(message_id, channel),
                guild_id=getattr(getattr(channel, "guild", None), "id", None), fail_if_not_exists=False,
            )

        @staticmethod
        def _is_reply_reference_rejected(err: Exception) -> bool:
            """Discord refused the reply anchor, not the message: deleted target (10008) or a 50035 form
            error on ``message_reference`` (system-message target, cross-channel thread starter).
            discord.py renders form errors as ``In <field path>: <reason>``, so other 50035 failures
            (content length, embeds, ...) are not retried without the reference."""
            if not starter_reply_reference:
                return base._is_reply_reference_rejected(err)
            err_text = str(err)
            if "error code: 10008" in err_text:
                return True
            return "error code: 50035" in err_text and (
                "In message_reference" in err_text or "Cannot reply to a system message" in err_text
            )

    DiscordExtensionsAdapter.__module__ = __name__
    DiscordExtensionsAdapter.__qualname__ = "DiscordExtensionsAdapter"
    return DiscordExtensionsAdapter


class _OverrideContext:
    """Plugin context proxy for the bundled ``register()``: its Discord registration goes through the real
    context with ``adapter_factory`` replaced; everything else is forwarded untouched."""

    def __init__(self, ctx: Any, adapter_class: type, base: type) -> None:
        self._ctx = ctx
        self._adapter_class = adapter_class
        self._base = base
        self.registered = False

    def register_platform(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("name") == PLATFORM_NAME and kwargs.get("adapter_factory") is self._base:
            kwargs["adapter_factory"] = self._adapter_class
            self.registered = True
        return self._ctx.register_platform(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


def register(ctx: Any) -> None:
    try:
        import plugins.platforms.discord as bundled
        from plugins.platforms.discord import adapter as adapter_module
    except Exception as exc:  # the bundled platform stays registered as is
        logger.warning(
            "discord-extensions: bundled Discord platform not importable (%s); the Discord adapter is NOT "
            "overridden, so %s are inactive", exc, " and ".join(FEATURE_DESCRIPTIONS.values()))
        return
    base = adapter_module.DiscordAdapter
    features, problems = check_dependencies(base)
    if problems:
        lost = [FEATURE_DESCRIPTIONS[f] for f in FEATURE_DESCRIPTIONS if f not in features]
        logger.warning(
            "discord-extensions: Hermes' Discord adapter changed (%s); disabled: %s. Run "
            "scripts/test-plugin.sh discord-extensions against this Hermes version and update the plugin.",
            "; ".join(problems), "; ".join(lost))
    proxy = _OverrideContext(ctx, build_adapter_class(adapter_module, features), base)
    bundled.register(proxy)
    if not proxy.registered:
        logger.warning(
            "discord-extensions: the bundled Discord register() no longer calls register_platform("
            "name='discord', adapter_factory=DiscordAdapter); the Discord adapter is NOT overridden, so %s "
            "are inactive", " and ".join(FEATURE_DESCRIPTIONS.values()))
