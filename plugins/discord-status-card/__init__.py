"""Hermes discord-status-card plugin: one status card per Discord turn, globally or in listed threads.

With ``display.status_cards: true``, every Discord turn gets ONE embed message, edited in place, instead of
per-tool progress messages, interim assistant messages and the long-running heartbeat. Alternatively,
``display.status_card_channels`` selects only threads under listed parent channels:

* while running: state (``💭 推論中`` / ``🔧 ツール実行中``) and the thread name, last event and start time
  (Discord ``<t:…:R>`` timestamps), the latest reasoning or interim text (~200 chars), the last tool, the tool
  count and the turn's tokens;
* at the end: one summary line (``✅ 完了`` / ``⚠️ 失敗`` / ``⏹ 中断`` · duration · tools · tokens).

Buttons: [詳細] answers the clicker ephemerally with the full log (tools with start, duration, status, preview
and error; interim texts; reasoning), attached as a file when long. [中断] runs the native ``/stop`` path; only
the requester or a user the Discord adapter's component allowlist admits may use it. The final reply is
delivered by the core as usual.

How (no fork change): the core task-card rail (``GatewayTurnProgressMixin._send_native_task_card_progress``) is
driven by the per-turn display flag ``_native_slack_task_cards`` and duck-typed adapter methods; only the flag's
resolution is Slack-specific. The plugin

* wraps ``_run_agent_display_settings(self, source)`` to set the flag, turn the text tool progress, interim
  messages and thinking off and report ``long_running_notifications`` as ``off`` for selected Discord turns;
* wraps ``_run_agent_bind_turn_wiring(self, turn_ctx, turn_runner, source, event_message_id,
  _native_slack_task_cards)`` to open the card once the turn's routing is known (not for scheduled heartbeat or
  muted diagnostic turns, which keep the core's quiet display);
* adds ``send_native_task_card_progress`` / ``stop_native_task_card_progress`` to
  ``plugins.platforms.discord.adapter.DiscordAdapter`` (inherited by discord-extensions' subclass): tool start
  and completion from the rail, the final edit when the rail stops at turn cleanup;
* observes ``post_api_request`` (tokens, reasoning), ``post_tool_call`` (duration, error),
  ``on_interim_message``, ``on_session_end`` (outcome) and ``agent_loop_stopped`` (``/stop``). Hooks run on side
  threads the agent waits for, so they only record under a lock and wake the card's writer on the gateway loop.
* wraps child construction/run to retain the originating thread and give every delegated child a separate
  card, context-local hooks, ID-correlated tool callbacks and a child-only stop button.

Both gateway wraps are installed on ``gateway.run.GatewayRunner`` itself (they delegate to the inherited
implementation), so they never touch the mixin methods other plugins (thread-only-display) wrap. Installed only
inside a gateway process: at ``register`` when ``gateway.run`` is imported, else on the first
``pre_gateway_dispatch``. Every private dependency is checked first; any problem is logged as one WARNING naming
the lost behaviour and nothing is installed (the core display stays). Card writes are debounced (one per
``EDIT_INTERVAL`` seconds).
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import importlib
import inspect
import io
import logging
import re
import sys
import threading
import time
import weakref
from contextlib import suppress
from typing import Any, Optional

__version__ = "0.3.0"

logger = logging.getLogger(__name__)

CONFIG_KEY = "status_card_channels"
EDIT_INTERVAL = 1.5  # seconds between card writes (Discord edit rate limits)
EXCERPT_CHARS = 200
DETAILS_TTL = 24 * 3600  # the [詳細] button outlives the turn this long (views are in memory only)
_LOG_LIMIT = 300  # log entries kept per card
_TEXT_LIMIT = 8000  # characters kept per interim / reasoning entry
_EMBED_TEXT_LIMIT = 4000  # longer details go out as a file

_MARK = "_discord_status_card_wrapped"
_LOST = ("so status cards are off: selected Discord turns keep the core display "
         "(per-tool progress messages, interim assistant messages, the long-running heartbeat)")
_DISPLAY_FIELDS = (
    "user_config", "platform_key", "progress_mode", "tool_progress_enabled", "interim_assistant_messages_enabled",
    "_thinking_enabled", "_native_slack_task_cards", "needs_progress_queue", "_display_surface_mode",
)
_TURN_FIELDS = (
    "source", "session_key", "user_config", "_progress_metadata", "mute_notification_reply", "scheduled_heartbeat",
)
_ADAPTER_MODULE = "plugins.platforms.discord.adapter"
_ADAPTER_SEAMS = (
    ("_resolve_channel", ["self", "channel_id"]),
    ("_run_simple_slash", ["self", "interaction", "command_text", "followup_msg"]),
    ("_component_live_auth", ["self", "interaction"]),
)
# Optional helpers: a problem loses one card detail, not the card.
_OPTIONAL = (
    ("agent.agent_runtime_helpers", "extract_reasoning", "the card shows no reasoning excerpt"),
    ("gateway.session_context", "bound_session_key",
     "the card shows no reasoning, interim text, token counts, tool errors or turn outcome"),
    ("agent.delegation_context", "is_delegated_child_context",
     "delegate_task children's interim text and tokens may show on the parent's card"),
)

_STATE_REASONING = "💭 推論中"
_STATE_TOOL = "🔧 ツール実行中"
_FINAL_STATES = {
    "completed": ("✅ 完了", 0x57F287),
    "failed": ("⚠️ 失敗", 0xED4245),
    "interrupted": ("⏹ 中断", 0x95A5A6),
}
_COLOR_RUNNING = 0x5865F2
_TOOL_ICONS = {"in_progress": "⏳", "ok": "✅", "error": "❌", "blocked": "🚫"}

_registrations = 0
_warned: set[str] = set()
_registry_lock = threading.Lock()
_cards: dict[str, "_Card"] = {}  # session key -> latest card of that session
_child_card: contextvars.ContextVar[Optional["_Card"]] = contextvars.ContextVar("discord_child_card", default=None)


# ── which turns ──────────────────────────────────────────────────────


def _configured_channels(user_config: Any) -> set[str]:
    display = user_config.get("display") if isinstance(user_config, dict) else None
    configured = display.get(CONFIG_KEY) if isinstance(display, dict) else None
    if not configured:
        return set()
    if isinstance(configured, (str, int)):
        configured = str(configured).split(",")
    if not isinstance(configured, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in configured if str(value).strip()}


def is_status_card_source(user_config: Any, source: Any) -> bool:
    """Select every Discord destination globally, or only threads under explicitly listed channels."""
    platform = getattr(source, "platform", None)
    if str(getattr(platform, "value", platform) or "").lower() != "discord":
        return False
    display = user_config.get("display") if isinstance(user_config, dict) else None
    if isinstance(display, dict) and display.get("status_cards") is True:
        return bool(getattr(source, "thread_id", None) or getattr(source, "chat_id", None))
    if not getattr(source, "thread_id", None):
        return False
    parent = str(getattr(source, "parent_chat_id", "") or "").strip()
    return bool(parent) and parent in _configured_channels(user_config)


def _tool_progress_explicitly_off(disp: Any) -> bool:
    """An operator who WROTE ``tool_progress: off`` gets no cards (the core's rule for Slack cards)."""
    if disp.progress_mode != "off":
        return False
    try:
        from agent.secret_scope import get_secret
        from gateway.display_config import resolve_tool_progress
        _mode, explicit = resolve_tool_progress(disp.user_config, disp.platform_key,
                                                get_secret("HERMES_TOOL_PROGRESS_MODE"))
        return bool(explicit)
    except Exception:
        return True


def _card_adapter(adapter: Any) -> bool:
    return _mine(getattr(type(adapter), "send_native_task_card_progress", None))


def card_display(disp: Any) -> Any:
    """*disp* (a ``_RunAgentDisplay``) switched to the card lane: no text progress / interim / thinking /
    heartbeat; the progress queue carries the rail's tool events."""
    core_surface = disp._display_surface_mode

    def _display_surface_mode(setting: str, **kwargs: Any) -> str:
        if setting == "long_running_notifications":
            return "off"
        return core_surface(setting, **kwargs)

    return dataclasses.replace(
        disp, tool_progress_enabled=False, interim_assistant_messages_enabled=False, _thinking_enabled=False,
        _native_slack_task_cards=True, needs_progress_queue=True, _display_surface_mode=_display_surface_mode,
    )


# ── formatting ───────────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _fmt_ms(duration_ms: Optional[float]) -> str:
    if duration_ms is None:
        return ""
    if duration_ms < 1000:
        return f"{int(duration_ms)}ms"
    return f"{duration_ms / 1000:.1f}s"


def _fmt_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def _one_line(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


# ── the card ─────────────────────────────────────────────────────────


class _Card:
    """One turn's card. Data is mutated under ``lock`` from hook threads and the loop; every Discord write
    happens in one writer task on ``loop``."""

    def __init__(self, *, adapter: Any, loop: asyncio.AbstractEventLoop, keys: tuple[str, ...], dest: str,
                 requester: str, metadata: Any) -> None:
        self.adapter, self.loop, self.keys, self.dest = adapter, loop, keys, dest
        self.requester, self.metadata = requester, metadata
        self.lock = threading.Lock()
        self.started = self.last_event = time.time()
        self.tools: dict[str, dict] = {}
        self.log: list[dict] = []
        self.excerpt = ""
        self.tokens_in = self.tokens_out = 0
        self.context_tokens: Optional[int] = None
        self.outcome: Optional[str] = None  # completed | failed | interrupted, from on_session_end
        self.exit_reason: Optional[str] = None
        self.stop_requested = False
        self.finished = False
        self.finished_at: Optional[float] = None
        self.thread_name: Optional[str] = None
        self.message: Any = None
        self.view: Any = None
        self._dirty = False
        self._writer: Optional[asyncio.Task] = None
        self._last_write = 0.0
        self._write_failed = False
        self.subagent_id = ""
        self.child_ref: Any = None
        self.goal = ""

    # events (any thread) ------------------------------------------------

    def _log(self, entry: dict) -> None:
        if len(self.log) < _LOG_LIMIT:
            self.log.append(entry)

    def _tool(self, call_id: str, name: str, now: float) -> dict:
        tool = self.tools.get(call_id)
        if tool is None:
            tool = self.tools[call_id] = {
                "id": call_id, "name": name, "preview": "", "started": now, "ended": None, "status": "in_progress",
                "duration_ms": None, "error": None, "seq": len(self.tools),
            }
            self._log({"kind": "tool", "at": now, "id": call_id})
        return tool

    def on_tool_start(self, call_id: str, name: str) -> None:
        with self.lock:
            self._tool(call_id, name, time.time())
            self.last_event = time.time()
        self.poke_threadsafe()

    def on_rail_tasks(self, tasks: Any) -> None:
        """Tool start / completion as the core rail publishes them (``{id, title, status}``)."""
        now = time.time()
        with self.lock:
            for task in tasks or ():
                call_id = str(task.get("id") or "")
                if not call_id:
                    continue
                name, _sep, preview = str(task.get("title") or "tool").partition(" - ")
                tool = self._tool(call_id, name, now)
                tool["name"] = name or tool["name"]
                if preview and not tool["preview"]:
                    tool["preview"] = preview
                status = task.get("status")
                if status in {"complete", "error"} and tool["ended"] is None:
                    tool["ended"] = now
                    if tool["status"] == "in_progress":
                        tool["status"] = "error" if status == "error" else "ok"
            self.last_event = now
        self.poke()

    def on_tool_end(self, call_id: str, name: str, duration_ms: Any, status: Any, error: Any) -> None:
        now = time.time()
        with self.lock:
            created = call_id not in self.tools
            tool = self._tool(call_id, name, now)
            if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
                tool["duration_ms"] = duration_ms
                if created:  # the rail's start event is still queued: date the start from the duration
                    tool["started"] = now - duration_ms / 1000
            tool["status"] = status if status in {"ok", "error", "blocked"} else "ok"
            tool["error"] = _one_line(error, 300) if error else None
            tool["ended"] = tool["ended"] or now
            self.last_event = now
        self.poke_threadsafe()

    def on_api(self, usage: Any, reasoning: Optional[str]) -> None:
        now = time.time()
        with self.lock:
            if isinstance(usage, dict):
                prompt = usage.get("prompt_tokens")
                if prompt is None:
                    prompt = usage.get("input_tokens")
                if prompt is not None:
                    self.context_tokens = int(prompt)
                    self.tokens_in += self.context_tokens
                self.tokens_out += int(usage.get("output_tokens") or 0)
            if reasoning and reasoning.strip():
                self.excerpt = reasoning
                self._log({"kind": "reasoning", "at": now, "text": reasoning[:_TEXT_LIMIT]})
            self.last_event = now
        self.poke_threadsafe()

    def on_interim(self, text: str) -> None:
        now = time.time()
        with self.lock:
            self.excerpt = text
            self._log({"kind": "interim", "at": now, "text": text[:_TEXT_LIMIT]})
            self.last_event = now
        self.poke_threadsafe()

    def on_outcome(self, *, completed: Any, failed: Any, interrupted: Any, reason: Any) -> None:
        with self.lock:
            self.outcome = "interrupted" if interrupted else "completed" if completed and not failed else "failed"
            self.exit_reason = str(reason) if reason else None
            self.last_event = time.time()
        self.poke_threadsafe()

    def on_stopped(self) -> None:
        with self.lock:
            self.stop_requested = True
        self.poke_threadsafe()

    # loop side ----------------------------------------------------------

    def finish(self) -> None:
        """The turn is over (the rail stopped at turn cleanup): one final edit, [中断] removed."""
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self.finished_at = time.time()
        if self.view is not None:
            self.view.turn_finished()
        self.poke()

    def poke(self) -> None:
        """Mark the card dirty and make sure the writer runs (call on the loop)."""
        self._dirty = True
        if self._writer is None or self._writer.done():
            self._writer = self.loop.create_task(self._write_loop())

    def poke_threadsafe(self) -> None:
        with suppress(RuntimeError):  # loop closed (gateway shutting down)
            self.loop.call_soon_threadsafe(self.poke)

    async def _write_loop(self) -> None:
        while self._dirty:
            if self._last_write:
                wait = self._last_write + EDIT_INTERVAL - self.loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._dirty = False
            await self._write()
            self._last_write = self.loop.time()

    async def _write(self) -> None:
        try:
            if self.message is None:
                channel = await self.adapter._resolve_channel(self.dest)
                self.thread_name = getattr(channel, "name", None)
                self.view = _view_class()(self)
                self.message = await channel.send(embed=self._embed(), view=self.view)
            else:
                await self.message.edit(embed=self._embed(), view=self.view)
        except Exception as exc:
            if not self._write_failed:
                self._write_failed = True
                logger.warning("discord-status-card: card write to %s failed: %s", self.dest, exc)
            else:
                logger.debug("discord-status-card: card write to %s failed again: %s", self.dest, exc)

    # rendering ----------------------------------------------------------

    def final_state(self) -> str:
        return self.outcome or ("interrupted" if self.stop_requested else "failed")

    def _token_summary(self) -> str:
        context = "未取得" if self.context_tokens is None else _fmt_tokens(self.context_tokens)
        return (f"context（直近API入力） {context} · ターン累計 入力 {_fmt_tokens(self.tokens_in)} / "
                f"出力 {_fmt_tokens(self.tokens_out)}（キャッシュ含む）")

    def _summary(self) -> str:
        label, _color = _FINAL_STATES[self.final_state()]
        return (f"{label} · {_fmt_duration((self.finished_at or time.time()) - self.started)} · "
                f"ツール {len(self.tools)} · {self._token_summary()}")

    def render(self) -> tuple[str, str, int]:
        """(title, description, color) of the card now."""
        with self.lock:
            label_name = f"{self.subagent_id} · {_one_line(self.goal, 160)}" if self.subagent_id else self.thread_name
            name = f" — {label_name}" if label_name else ""
            if self.finished:
                label, color = _FINAL_STATES[self.final_state()]
                return f"{label}{name}"[:256], self._summary(), color
            running = any(tool["ended"] is None for tool in self.tools.values())
            lines = [f"最終イベント <t:{int(self.last_event)}:R> · 開始 <t:{int(self.started)}:R>"
                     f"（{_fmt_duration(self.last_event - self.started)}）"]
            if self.excerpt:
                lines.append("> " + _one_line(self.excerpt, EXCERPT_CHARS))
            if self.tools:
                last = max(self.tools.values(), key=lambda tool: (tool["started"], tool["seq"]))
                line = f"{_TOOL_ICONS.get(last['status'], '⏳')} {last['name']}"
                if last["preview"]:
                    line += f" — {_one_line(last['preview'], 120)}"
                if last["ended"] is not None:
                    line += f" · {_fmt_ms(last['duration_ms']) or _fmt_duration(last['ended'] - last['started'])}"
                lines.append(line)
            lines.append(f"ツール {len(self.tools)} · {self._token_summary()}")
            state = _STATE_TOOL if running else _STATE_REASONING
            return f"{state}{name}"[:256], "\n".join(lines), _COLOR_RUNNING

    def _embed(self) -> Any:
        import discord
        title, description, color = self.render()
        return discord.Embed(title=title, description=description, color=color)

    def details_text(self) -> str:
        with self.lock:
            head = f"開始 {_clock(self.started)} · "
            head += self._summary() if self.finished else _STATE_TOOL if any(
                tool["ended"] is None for tool in self.tools.values()) else _STATE_REASONING
            if self.finished and self.exit_reason:
                head += f" · {self.exit_reason}"
            lines = [head]
            for entry in self.log:
                at = _clock(entry["at"])
                if entry["kind"] == "tool":
                    tool = self.tools[entry["id"]]
                    status = tool["status"]
                    icon = "⏹" if self.finished and tool["ended"] is None else _TOOL_ICONS.get(status, "⏳")
                    line = f"{_clock(tool['started'])} {icon} {tool['name']}"
                    if tool["preview"]:
                        line += f" — {tool['preview']}"
                    if tool["ended"] is not None:
                        line += f" · {_fmt_ms(tool['duration_ms']) or _fmt_duration(tool['ended'] - tool['started'])}"
                    if tool["error"]:
                        line += f" · {tool['error']}"
                    lines.append(line)
                elif entry["kind"] == "interim":
                    lines.append(f"{at} 💬 {entry['text']}")
                else:
                    lines.append(f"{at} 💭 {entry['text']}")
            if len(self.log) >= _LOG_LIMIT:
                lines.append(f"（ログは先頭 {_LOG_LIMIT} 件まで）")
            return "\n".join(lines)

    # buttons --------------------------------------------------------------

    async def send_details(self, interaction: Any) -> None:
        import discord
        text = self.details_text()
        title = f"詳細 — {self.subagent_id} · {self.goal}" if self.subagent_id else (
            f"詳細 — {self.thread_name}" if self.thread_name else "詳細")
        if len(text) <= _EMBED_TEXT_LIMIT:
            await interaction.response.send_message(
                embed=discord.Embed(title=title[:256], description=text), ephemeral=True)
            return
        await interaction.response.send_message(
            content=f"{title}（全ログ {len(self.log)} 件、添付）",
            file=discord.File(io.BytesIO(text.encode("utf-8")), filename="status-card-log.txt"), ephemeral=True)

    def may_stop(self, interaction: Any) -> bool:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        if user_id and user_id == self.requester:
            return True
        try:
            from plugins.platforms.discord.adapter_component_auth import _component_check_auth
            adapter = self.adapter
            return bool(_component_check_auth(
                interaction, getattr(adapter, "_allowed_user_ids", set()), getattr(adapter, "_allowed_role_ids", set()),
                live_auth=adapter._component_live_auth))
        except Exception:
            logger.debug("discord-status-card: stop authorization check failed", exc_info=True)
            return False

    async def request_stop(self, interaction: Any) -> None:
        if not self.may_stop(interaction):
            await interaction.response.send_message(
                "中断できるのは依頼したユーザーと許可されたユーザーだけです。", ephemeral=True)
            return
        if self.finished:
            await interaction.response.send_message("このターンはもう終わっています。", ephemeral=True)
            return
        if self.subagent_id:
            from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock, interrupt_subagent
            with _active_subagents_lock:
                record = _active_subagents.get(self.subagent_id)
                live = record is not None and record.get("agent") is self.child_ref()
            stopped = live and interrupt_subagent(self.subagent_id)
            await interaction.response.send_message(
                "この子エージェントに中断を要求しました。" if stopped else "この子エージェントはもう実行中ではありません。",
                ephemeral=True)
            return
        await self.adapter._run_simple_slash(interaction, "/stop")


_VIEW_CLASS: Any = None


def _view_class() -> Any:
    """The card's ``discord.ui.View`` (defined on first use: CLI processes never import discord for this)."""
    global _VIEW_CLASS
    if _VIEW_CLASS is None:
        import discord

        class StatusCardView(discord.ui.View):
            def __init__(self, card: _Card) -> None:
                # Live for the whole turn; afterwards [詳細] stays DETAILS_TTL (see turn_finished).
                super().__init__(timeout=DETAILS_TTL if card.finished else None)
                self.card = card
                details = discord.ui.Button(label="詳細", style=discord.ButtonStyle.secondary)
                details.callback = self._on_details
                self.add_item(details)
                self.stop_button = None
                if not card.finished:
                    self.stop_button = discord.ui.Button(label="中断", style=discord.ButtonStyle.danger)
                    self.stop_button.callback = self._on_stop
                    self.add_item(self.stop_button)

            def turn_finished(self) -> None:
                if self.stop_button is not None:
                    self.remove_item(self.stop_button)
                    self.stop_button = None
                self.timeout = DETAILS_TTL  # (re)armed when the final edit re-stores the view

            async def on_timeout(self) -> None:
                if self.card.message is not None:
                    with suppress(Exception):
                        await self.card.message.edit(view=None)

            async def _on_details(self, interaction: Any) -> None:
                await self.card.send_details(interaction)

            async def _on_stop(self, interaction: Any) -> None:
                await self.card.request_stop(interaction)

        _VIEW_CLASS = StatusCardView
    return _VIEW_CLASS


def _open_card(adapter: Any, turn_ctx: Any, source: Any) -> None:
    dest = str(getattr(source, "thread_id", None) or getattr(source, "chat_id", ""))
    # The turn's session key, and the one bound in this task's context (what the hooks read back).
    keys = tuple(dict.fromkeys(key for key in (str(turn_ctx.session_key or ""), _bound_session_key()) if key))
    card = _Card(adapter=adapter, loop=asyncio.get_running_loop(), keys=keys or (dest,), dest=dest,
                 requester=str(getattr(source, "user_id", "") or ""), metadata=turn_ctx._progress_metadata)
    with _registry_lock:
        previous = {_cards[key] for key in card.keys if key in _cards}
        _cards.update(dict.fromkeys(card.keys, card))
    for old in previous:
        old.finish()  # e.g. the outer turn of a queued follow-up: its rail stops later
    card.poke()


def _card_for_rail(metadata: Any) -> Optional[_Card]:
    """The card of the turn whose rail calls (its progress metadata is that turn's own dict)."""
    with _registry_lock:
        cards = list(_cards.values())
    for card in cards:
        if metadata is not None and card.metadata is metadata:
            return card
    key = _bound_session_key()
    card = _cards.get(key) if key else None
    return card if card is not None and card.metadata is None and not card.finished else None


# ── adapter methods (installed on DiscordAdapter) ─────────────────────


async def send_native_task_card_progress(self, chat_id, tasks, *, title=None, reply_to=None, metadata=None,
                                         fallback_text=None):
    """Core task-card rail: tool start / completion. Always succeeds, so the rail never falls back to text."""
    from gateway.platforms.base import SendResult
    card = _card_for_rail(metadata)
    if card is not None:
        card.on_rail_tasks(tasks)
    return SendResult(success=True, message_id=str(card.message.id) if card and card.message else None)


async def stop_native_task_card_progress(self, chat_id, *, reply_to=None, metadata=None):
    """Core task-card rail, at turn cleanup: the card's final edit."""
    card = _card_for_rail(metadata)
    if card is not None:
        card.finish()
        if card.outcome is not None:
            with _registry_lock:
                for key in card.keys:
                    if _cards.get(key) is card:
                        del _cards[key]


_ADAPTER_METHODS = {
    "send_native_task_card_progress": send_native_task_card_progress,
    "stop_native_task_card_progress": stop_native_task_card_progress,
}


# ── hooks ────────────────────────────────────────────────────────────


def _optional(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name, None)
    except Exception:
        return None


def _bound_session_key() -> str:
    getter = _optional("gateway.session_context", "bound_session_key")
    return str(getter() or "") if callable(getter) else ""


def _in_delegate_child() -> bool:
    check = _optional("agent.delegation_context", "is_delegated_child_context")
    return bool(check()) if callable(check) else False


def _hook_card() -> Optional[_Card]:
    """The executing child gets its own card, never the latest parent turn's card."""
    if _in_delegate_child():
        card = _child_card.get()
        return card if card is not None and not card.finished else None
    key = _bound_session_key()
    card = _cards.get(key) if key else None
    return card if card is not None and card.outcome is None else None


def _post_api_request(**kwargs: Any) -> None:
    card = _hook_card()
    if card is None:
        return
    reasoning = None
    extract = _optional("agent.agent_runtime_helpers", "extract_reasoning")
    message = kwargs.get("assistant_message")
    if callable(extract) and message is not None:
        with suppress(Exception):
            reasoning = extract(None, message)
    card.on_api(kwargs.get("usage"), reasoning)


def _post_tool_call(**kwargs: Any) -> None:
    card = _hook_card()
    if card is not None and kwargs.get("tool_call_id"):
        card.on_tool_end(str(kwargs["tool_call_id"]), str(kwargs.get("tool_name") or "tool"),
                         kwargs.get("duration_ms"), kwargs.get("status"), kwargs.get("error_message"))


def _on_interim_message(**kwargs: Any) -> None:
    card = _hook_card()
    text = kwargs.get("text")
    if card is not None and isinstance(text, str) and text.strip():
        card.on_interim(text)


def _on_session_end(**kwargs: Any) -> None:
    card = _hook_card()
    if card is not None:
        card.on_outcome(completed=kwargs.get("completed"), failed=kwargs.get("failed"),
                        interrupted=kwargs.get("interrupted"), reason=kwargs.get("turn_exit_reason"))


def _agent_loop_stopped(**kwargs: Any) -> None:
    card = _cards.get(str(kwargs.get("session_key") or ""))
    if card is not None and card.outcome is None:
        card.on_stopped()


async def _pre_gateway_dispatch(**_kwargs: Any) -> None:
    if _registrations:
        install()
    return None


# ── install / private-API guards ─────────────────────────────────────


def _params(func: Any) -> Optional[list[str]]:
    try:
        return list(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        return None


def _mine(func: Any) -> bool:
    return func is not None and getattr(func, _MARK, None) is func


def _mark(func: Any) -> Any:
    setattr(func, _MARK, func)
    return func


def _wrap_display(cls: type, own: Any) -> Any:
    def _run_agent_display_settings(self, source):
        disp = own(self, source) if own is not None else super(cls, self)._run_agent_display_settings(source)
        try:
            if (is_status_card_source(disp.user_config, source) and not _tool_progress_explicitly_off(disp)
                    and _card_adapter(self._delivery_adapter_for(source))):
                return card_display(disp)
        except Exception:
            logger.debug("discord-status-card: display rewrite failed", exc_info=True)
        return disp

    _run_agent_display_settings._discord_status_card_original = own
    return _mark(_run_agent_display_settings)


def _wrap_bind(cls: type, own: Any) -> Any:
    def _run_agent_bind_turn_wiring(self, turn_ctx, turn_runner, source, event_message_id, _native_slack_task_cards):
        if own is not None:
            result = own(self, turn_ctx, turn_runner, source, event_message_id, _native_slack_task_cards)
        else:
            result = super(cls, self)._run_agent_bind_turn_wiring(
                turn_ctx, turn_runner, source, event_message_id, _native_slack_task_cards)
        try:
            if (_native_slack_task_cards and not turn_ctx.mute_notification_reply and not turn_ctx.scheduled_heartbeat
                    and is_status_card_source(turn_ctx.user_config, source)):
                adapter = self._delivery_adapter_for(source)
                if _card_adapter(adapter):
                    _open_card(adapter, turn_ctx, source)
        except Exception:
            logger.warning("discord-status-card: could not open the status card", exc_info=True)
        return result

    _run_agent_bind_turn_wiring._discord_status_card_original = own
    return _mark(_run_agent_bind_turn_wiring)


def _wrap_child_build(original: Any) -> Any:
    @functools.wraps(original)
    def build(*args: Any, **kwargs: Any) -> Any:
        route = _hook_card()
        child = original(*args, **kwargs)
        # Capture before dispatch: background children may start after the parent's rail has retired.
        if route is not None:
            child._discord_status_card_route = (route.adapter, route.loop, route.dest, route.requester)
        return child
    build._discord_status_card_original = original
    return _mark(build)


def _wrap_child_run(original: Any) -> Any:
    @functools.wraps(original)
    def run(task_index, goal, child=None, parent_agent=None, **kwargs):
        route = getattr(child, "_discord_status_card_route", None)
        if route is None or route[1].is_closed():
            return original(task_index, goal, child, parent_agent, **kwargs)
        adapter, loop, dest, requester = route
        card = _Card(adapter=adapter, loop=loop, keys=(), dest=dest, requester=requester, metadata=None)
        card.subagent_id = str(getattr(child, "_subagent_id", "") or "")
        card.child_ref = weakref.ref(child)
        card.goal = goal
        start = getattr(child, "tool_start_callback", None)
        complete = getattr(child, "tool_complete_callback", None)
        progress = getattr(child, "tool_progress_callback", None)
        completion = threading.local()

        def tool_progress(event_type, name=None, *args, **kw):
            # Codex emits its authoritative verdict immediately before the ID-bearing completion.
            # Keep it thread-local: concurrent calls of the same tool must never exchange verdicts.
            if event_type == "tool.completed":
                completion.value = (name, kw)
            if progress is not None:
                return progress(event_type, name, *args, **kw)

        if progress is not None:
            functools.update_wrapper(tool_progress, progress)

        def tool_start(call_id, name, args):
            completion.value = None
            card.on_tool_start(call_id, name)
            if start is not None:
                start(call_id, name, args)

        def tool_complete(call_id, name, args, result):
            from agent.display import _detect_tool_failure
            meta = getattr(completion, "value", None)
            completion.value = None
            duration_ms = None
            if meta is not None and meta[0] == name and "is_error" in meta[1]:
                is_error, reason = meta[1]["is_error"], None
                duration = meta[1].get("duration")
                if isinstance(duration, (int, float)):
                    duration_ms = duration * 1000
            else:
                is_error, reason = _detect_tool_failure(name, result)
            card.on_tool_end(call_id, name, duration_ms, "error" if is_error else "ok", reason if is_error else None)
            if complete is not None:
                complete(call_id, name, args, result)

        child.tool_start_callback = tool_start
        child.tool_complete_callback = tool_complete
        child.tool_progress_callback = tool_progress
        token = _child_card.set(card)
        card.poke_threadsafe()
        try:
            result = original(task_index, goal, child, parent_agent, **kwargs)
            # The runner owns the final verdict (also exceptions, timeouts and schema retries).
            card.on_outcome(completed=result.get("status") == "completed" and not result.get("truncated"),
                            failed=result.get("status") not in {"completed", "interrupted"},
                            interrupted=result.get("status") == "interrupted", reason=result.get("exit_reason"))
            return result
        finally:
            child.tool_start_callback = start
            child.tool_complete_callback = complete
            child.tool_progress_callback = progress
            _child_card.reset(token)
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(card.finish)
    run._discord_status_card_original = original
    return _mark(run)


_CHILD_SEAMS = (
    ("_build_child_agent", ("task_index", "goal", "parent_agent"), _wrap_child_build),
    ("_run_single_child", ("task_index", "goal", "child", "parent_agent"), _wrap_child_run),
)


def _install_children() -> None:
    try:
        module = importlib.import_module("tools.delegate_tool")
        registry = importlib.import_module("tools.delegate_tool_registry")
    except Exception as exc:
        _warn_once(f"delegation modules are not importable ({exc})", "so child status cards are off")
        return
    for mod, name in (("agent.display", "_detect_tool_failure"),
                      ("agent.delegation_context", "is_delegated_child_context"),
                      ("tools.delegate_tool_registry", "interrupt_subagent")):
        if not callable(_optional(mod, name)):
            _warn_once(f"{mod}.{name} is missing", "so child status cards are off")
            return
    if (not isinstance(getattr(registry, "_active_subagents", None), dict)
            or not hasattr(getattr(registry, "_active_subagents_lock", None), "__enter__")):
        _warn_once("the live subagent registry is missing", "so child status cards are off")
        return
    for name, required, _factory in _CHILD_SEAMS:
        method = getattr(module, name, None)
        params = _params(method) if callable(method) else None
        if params is None or not all(key in params for key in required):
            _warn_once(f"tools.delegate_tool.{name} is missing or re-signed", "so child status cards are off")
            return
    for name, _required, factory in _CHILD_SEAMS:
        method = getattr(module, name)
        if not _mine(method):
            setattr(module, name, factory(method))


_RUNNER_SEAMS = (
    ("_run_agent_display_settings", ["self", "source"], _wrap_display),
    ("_run_agent_bind_turn_wiring",
     ["self", "turn_ctx", "turn_runner", "source", "event_message_id", "_native_slack_task_cards"], _wrap_bind),
)


def _dataclass_problem(cls: Any, label: str, fields: tuple[str, ...]) -> Optional[str]:
    if cls is None or not dataclasses.is_dataclass(cls):
        return f"{label} is missing or not a dataclass"
    names = {field.name for field in dataclasses.fields(cls)}
    missing = [name for name in fields if name not in names]
    return f"{label} lacks fields {missing!r}" if missing else None


def _check_core() -> tuple[list[str], Any, Any]:
    """Problems with the required private seams, plus (GatewayRunner, DiscordAdapter) when usable."""
    problems: list[str] = []
    runner_cls = getattr(sys.modules.get("gateway.run"), "GatewayRunner", None)
    if runner_cls is None:
        return ["gateway.run.GatewayRunner is missing"], None, None
    for name, params, _factory in _RUNNER_SEAMS:
        # What the (installed or future) wrapper delegates to: GatewayRunner's own method or the inherited one.
        current = runner_cls.__dict__.get(name)
        target = current._discord_status_card_original if _mine(current) else current
        if target is None:
            target = next((klass.__dict__[name] for klass in runner_cls.__mro__[1:] if name in klass.__dict__), None)
        if not callable(target):
            problems.append(f"GatewayRunner.{name} is missing")
        elif _params(target) != params:
            problems.append(f"GatewayRunner.{name} signature changed ({_params(target)!r}, expected {params!r})")
    problem = _dataclass_problem(getattr(runner_cls, "_RunAgentDisplay", None), "GatewayRunner._RunAgentDisplay",
                                 _DISPLAY_FIELDS)
    if problem:
        problems.append(problem)
    if not callable(getattr(runner_cls, "_delivery_adapter_for", None)):
        problems.append("GatewayRunner._delivery_adapter_for is missing")
    try:
        from gateway.turn_context import TurnContext
    except Exception as exc:
        TurnContext = None
        problems.append(f"gateway.turn_context not importable ({exc})")
    if TurnContext is not None:
        problem = _dataclass_problem(TurnContext, "gateway.turn_context.TurnContext", _TURN_FIELDS)
        if problem:
            problems.append(problem)
    try:
        from gateway.run_turn_progress import GatewayTurnProgressMixin
        for name in ("send_progress_messages", "_send_native_task_card_progress"):
            if not callable(getattr(GatewayTurnProgressMixin, name, None)):
                problems.append(f"the task-card rail GatewayTurnProgressMixin.{name} is missing")
    except Exception as exc:
        problems.append(f"gateway.run_turn_progress not importable ({exc})")
    adapter_cls = None
    try:
        import discord  # noqa: F401
        adapter_cls = importlib.import_module(_ADAPTER_MODULE).DiscordAdapter
        from plugins.platforms.discord.adapter_component_auth import _component_check_auth  # noqa: F401
    except Exception as exc:
        problems.append(f"the Discord adapter is not importable ({type(exc).__name__}: {exc})")
    if adapter_cls is not None:
        for name, params in _ADAPTER_SEAMS:
            method = getattr(adapter_cls, name, None)
            if not callable(method):
                problems.append(f"DiscordAdapter.{name} is missing")
            elif _params(method) != params:
                problems.append(f"DiscordAdapter.{name} signature changed ({_params(method)!r}, expected {params!r})")
        for name in _ADAPTER_METHODS:
            existing = getattr(adapter_cls, name, None)
            if existing is not None and not _mine(existing):
                problems.append(f"DiscordAdapter already defines {name} (Hermes has its own Discord task cards now)")
    return problems, runner_cls, adapter_cls


def _warn_once(problem: str, lost: str) -> None:
    if problem in _warned:
        return
    _warned.add(problem)
    logger.warning("discord-status-card: %s, %s. Update the plugin for this Hermes version.", problem, lost)


def install() -> None:
    """Install every seam, or none (one WARNING per problem); requires ``gateway.run`` in this process."""
    if "gateway.run" not in sys.modules:
        return
    problems, runner_cls, adapter_cls = _check_core()
    if problems:
        for problem in problems:
            _warn_once(problem, _LOST)
        return
    for module, name, lost in _OPTIONAL:
        if not callable(_optional(module, name)):
            _warn_once(f"{module}.{name} is missing", f"so {lost}")
    for name, _params_expected, factory in _RUNNER_SEAMS:
        current = runner_cls.__dict__.get(name)
        if not _mine(current):
            setattr(runner_cls, name, factory(runner_cls, current))
    for name, func in _ADAPTER_METHODS.items():
        if not _mine(adapter_cls.__dict__.get(name)):
            setattr(adapter_cls, name, _mark(func))
    _install_children()


def uninstall() -> None:
    module = sys.modules.get("tools.delegate_tool")
    if module is not None:
        for name, *_rest in _CHILD_SEAMS:
            method = getattr(module, name, None)
            if _mine(method):
                setattr(module, name, method._discord_status_card_original)
    runner_cls = getattr(sys.modules.get("gateway.run"), "GatewayRunner", None)
    if runner_cls is not None:
        for name, *_rest in _RUNNER_SEAMS:
            current = runner_cls.__dict__.get(name)
            if _mine(current):
                original = current._discord_status_card_original
                if original is not None:
                    setattr(runner_cls, name, original)
                else:
                    delattr(runner_cls, name)
    adapter_cls = getattr(sys.modules.get(_ADAPTER_MODULE), "DiscordAdapter", None)
    if adapter_cls is not None:
        for name in _ADAPTER_METHODS:
            if _mine(adapter_cls.__dict__.get(name)):
                delattr(adapter_cls, name)


def _on_unload() -> None:
    global _registrations
    _registrations = max(0, _registrations - 1)
    if not _registrations:
        uninstall()


def register(ctx: Any) -> None:
    global _registrations
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("post_api_request", _post_api_request)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("on_interim_message", _on_interim_message)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("agent_loop_stopped", _agent_loop_stopped)
    _registrations += 1
    ctx.on_unload(_on_unload)
    install()
    logger.info("discord-status-card v%s armed", __version__)


__all__ = ["register", "install", "uninstall", "is_status_card_source", "card_display", "__version__"]
