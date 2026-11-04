# discord-status-card

A Discord turn shows **one status card** (an embed edited in place) instead of per-tool progress messages,
interim assistant messages and the 180 s "still working" heartbeat. The final reply is delivered as a separate
message, as before. Enable it globally, or opt in only threads under listed channels.

## Config

```yaml
display:
  status_cards: true   # all Discord turns: threads, channel replies and DMs
```

```bash
hermes config set display.status_cards true
```

Global mode covers every Discord destination the adapter can send to, including future channels, non-thread
channel replies and DMs. It does not enable cards on other platforms. Cards go to the turn's reply destination:
the thread when present, otherwise the channel/DM.

To keep the narrower opt-in instead, omit `status_cards` (or set it to `false`) and set
`display.status_card_channels: ["1501181227359539401"]` (parent channel IDs: YAML list, int, or `"id1, id2"`).
Only Discord **threads** under those parents get cards, including threads Hermes auto-creates (`auto_thread`);
parent-channel replies and other channels keep the core display. An empty list enables no channels.
`status_cards` must be a YAML boolean; global mode does not require or use a channel list.

An explicit `tool_progress: off` keeps cards off (the core's rule for Slack task cards). Scheduled heartbeat
turns and muted diagnostic turns keep the core's quiet display (no card). Button authorization, delegated-child
cards and final-reply delivery are unchanged in global mode.

## The card

While the turn runs:

```
💭 推論中 — <thread name>                       (🔧 ツール実行中 while a tool runs)
最終イベント 3秒前 · 開始 1分前（52s）            (Discord <t:…:R> timestamps)
> latest reasoning or interim text (~200 chars; line omitted when there is none)
✅ terminal — ls -la · 120ms                     (last tool: ✅ / ⏳ / ❌ / 🚫, preview, duration)
ツール 6 · トークン in 12.3k / out 456            (this turn: prompt tokens incl. cache / output tokens)
[詳細] [中断]
```

At the end, one final edit: `✅ 完了 · 32s · ツール 6 · in 12.3k / out 456` (or `⚠️ 失敗`, `⏹ 中断`), [中断]
removed, [詳細] kept for 24 h (views are in memory: after a gateway restart old buttons stop working).

- **[詳細]**: an ephemeral reply to whoever clicks, with the whole turn: each tool (start time, status, preview,
  duration, error), every interim text and reasoning excerpt in order, and the outcome / exit reason. Over 4000
  characters it is attached as `status-card-log.txt`.
- **[中断]**: runs the native `/stop` (`DiscordAdapter._run_simple_slash(interaction, "/stop")`, the slash
  command's own authorization included). Allowed for the user who sent the turn and for users the Discord
  adapter's component allowlist admits (`DISCORD_ALLOWED_USERS` / roles / pairing, the same check as approval
  buttons); anyone else gets an ephemeral refusal.

Writes are debounced to one per 1.5 s (`EDIT_INTERVAL`); the relative timestamps update client-side without
edits.

Outcome: `on_session_end`'s `interrupted` → 中断, `completed` → 完了, anything else (failure, iteration budget
exhausted) → 失敗. When the turn ends without `on_session_end` the card shows 中断 after an `agent_loop_stopped`
(`/stop`, `/new`), else 失敗; a late `on_session_end` corrects it.

### Delegated tasks (v0.2.0)

Each `delegate_task` child gets its own card in the commissioning Discord thread, titled with its subagent ID
and task goal. Synchronous, background and nested children use the same display; parallel children never share
tool IDs, logs or token counts. A parent's card can finish before its background children: their cards continue
updating until their own runners return. No child cards are created for parents without a status card.

Child cards have the same [詳細] button. Their [中断] button uses `interrupt_subagent` for that exact live child,
not the thread's `/stop`; the requester/allowlist authorization is unchanged. The core may also stop descendants
of that child, but not its parent or siblings. Completion, failure, interruption and iteration exhaustion come
from the child's runner result, including failures that occur before a session-end hook.

Tools use the ID-bearing start/complete callbacks; Codex app-server completion verdicts and durations come from
its progress callback, paired on the emitting thread with the immediately following ID-bearing callback.

## How it works

The core already has a per-turn task-card rail (`GatewayTurnProgressMixin._send_native_task_card_progress`):
once the display flag `_native_slack_task_cards` is set, text tool progress stops, the ID-correlated tool
start/complete callbacks feed the progress queue, and the rail calls duck-typed adapter methods, ending with a
stop call at turn cleanup. Only the flag's resolution is Slack-specific. The plugin:

| Part | What |
|---|---|
| `GatewayRunner._run_agent_display_settings(self, source)` (wrapped) | selected Discord turns: `_native_slack_task_cards=True`, `needs_progress_queue=True`, `tool_progress_enabled=False`, `interim_assistant_messages_enabled=False`, `_thinking_enabled=False`, and `_display_surface_mode("long_running_notifications")` → `off` (only when the turn's adapter carries the card methods) |
| `GatewayRunner._run_agent_bind_turn_wiring(self, turn_ctx, turn_runner, source, event_message_id, _native_slack_task_cards)` (wrapped) | opens the card when the turn really uses the card lane, keyed by the session key and the turn's progress metadata dict |
| `DiscordAdapter.send_native_task_card_progress` / `stop_native_task_card_progress` (added) | rail input: tool start / completion, then the final edit; always report success so the rail never falls back to text |
| hooks | `post_api_request` (usage, reasoning via `extract_reasoning`), `post_tool_call` (duration, status, error), `on_interim_message`, `on_session_end` (outcome), `agent_loop_stopped` (`/stop`) |

Both wraps are set on `GatewayRunner` itself and delegate to the inherited mixin method, so they stack with
`thread-only-display` (which wraps `GatewayTurnMixin`) and either plugin can be unloaded independently. The
methods are added to the bundled `plugins.platforms.discord.adapter.DiscordAdapter`, so discord-extensions'
subclass inherits them; the adapter class is not swapped. Hooks run on threads the agent waits for: they only
record under a lock and wake the card's writer on the gateway loop (`loop.call_soon_threadsafe`). Child hooks
use a separate context-local card, never the parent's session-key lookup.

Installed only in a gateway process: at `register` when `gateway.run` is already imported, else on the first
`pre_gateway_dispatch`. `plugins.isolation: in_process` (the default) is required.

## Private dependencies and breakage detection

Checked before anything is installed. Any problem leaves the core untouched and logs one WARNING per problem:

```
discord-status-card: DiscordAdapter._resolve_channel is missing, so status cards are off: turns in threads under
display.status_card_channels keep the core display (per-tool progress messages, interim assistant messages, the
long-running heartbeat). Update the plugin for this Hermes version.
```

| Seam | Used for |
|---|---|
| `GatewayRunner._run_agent_display_settings(self, source)`, `GatewayRunner._RunAgentDisplay` fields `user_config`, `platform_key`, `progress_mode`, `tool_progress_enabled`, `interim_assistant_messages_enabled`, `_thinking_enabled`, `_native_slack_task_cards`, `needs_progress_queue`, `_display_surface_mode` | the per-thread switch |
| `GatewayRunner._run_agent_bind_turn_wiring(...)`, `TurnContext` fields `source`, `session_key`, `user_config`, `_progress_metadata`, `mute_notification_reply`, `scheduled_heartbeat`, `GatewayRunner._delivery_adapter_for` | opening the card |
| `GatewayTurnProgressMixin.send_progress_messages` / `_send_native_task_card_progress` | the rail calling `send_/stop_native_task_card_progress` |
| `DiscordAdapter._resolve_channel(channel_id)`, `_run_simple_slash(interaction, command_text, followup_msg)`, `_component_live_auth(interaction)`, `_allowed_user_ids`, `_allowed_role_ids`; `adapter_component_auth._component_check_auth` | embed send/edit, [中断] |
| `tools.delegate_tool._build_child_agent`, `_run_single_child` (wrapped) | capture the originating route before background dispatch; open/finalize each child's card independently |
| `AIAgent.tool_start_callback`, `tool_complete_callback`, `tool_progress_callback`; `agent.display._detect_tool_failure` | child tool lifecycle, including native Codex events |
| `tools.delegate_tool_registry._active_subagents`, `_active_subagents_lock`, `interrupt_subagent` | exact live-child identity and child-only stop |

Optional (a problem is a WARNING; the card stays without that detail): `agent.agent_runtime_helpers.extract_reasoning`,
`gateway.session_context.bound_session_key`, `agent.delegation_context.is_delegated_child_context`.

Following upstream: `scripts/test-plugin.sh discord-status-card` against the new `hermes-ref.env`.

- `test_core_*` fail when the core itself changed what a listed thread shows without the plugin, or the rail no
  longer calls the adapter methods (re-read `run_turn_progress.py` first).
- `test_listed_thread_*`, `test_*_card*` fail when the wraps, the rail or the hooks moved.
- `test_registers_on_this_hermes_without_a_warning` fails when the startup check would warn on the rpi;
  `test_missing_*` / `test_resigned_*` / `test_display_dataclass_change_*` prove the fallback.
- `test_coexists_with_thread_only_display` fails when the two wraps start interfering.
- `test_child_cards_survive_parent_and_isolate_parallel_events` covers synchronous/background child cards,
  repeated tool IDs in parallel children, parent retirement, separate usage/logs, and completed/failed outcomes.
- `test_child_stop_targets_only_authorized_child` covers unauthorized refusal and the real hard-interrupt state
  on the child without interrupting its parent.
- `test_codex_child_tool_completion_without_post_tool_hook` drives the core Codex event bridge to prove failed
  command completion reaches the child card without a `post_tool_call` hook.

## Changelog

- **0.3.0**: add `display.status_cards: true` for all Discord turns, including non-thread replies and DMs.
  Keep channel-scoped opt-in when global mode is off, and preserve quiet turns, explicit progress opt-out,
  card contents, button authorization and per-child cards.
