# thread-only-display

`display.thread_only_channels` for the Hermes gateway: in a listed channel a turn shows only the
final answer, and transient output stays inside threads. Replaces fork commit `5327a5da17`
(`feat(gateway): display.thread_only_channels keeps transient output in threads`).

## Config

Same key as the fork (no config change):

```yaml
display:
  thread_only_channels: ["1509077323490791515"]   # YAML list, int, or "id1, id2"
```

For a source whose `chat_id` is listed and that has no `thread_id`:

- tool progress off (`log` is kept: it writes `tool_calls.log`, not chat),
- interim assistant messages off, thinking progress off (`needs_progress_queue` recomputed; Slack
  native task cards keep their own verdict, as in the fork),
- the `show_reasoning` block is not prepended to the final answer.

Threads under a listed channel are unaffected.

## How it works

Two private gateway methods are wrapped on their defining mixins (so `GatewayRunner` inherits them):

| Seam | Wrap |
| --- | --- |
| `gateway.run_turn.GatewayTurnMixin._run_agent_display_settings(self, source)` | `dataclasses.replace` on the returned `GatewayRunner._RunAgentDisplay`, using its `user_config` (the same profile-scoped config the core resolved) |
| `gateway.run_turn_persistence.GatewayTurnPersistenceMixin._hmwa_prepend_reasoning(self, agent_result, response, source, _intentional_silence)` | returns `response` unchanged for a listed channel (config from `gateway.run._load_gateway_config()`, as the core does) |

**Install timing**: the wraps are installed only in a gateway process. `register` installs them when
`gateway.run` is already imported; otherwise installation is deferred to the first
`pre_gateway_dispatch` hook, which every inbound turn passes before its display settings are resolved.
The plugin never imports the gateway into a CLI process. `plugins.isolation: in_process` (the default)
is required.

## Private dependencies and breakage detection

At install each target is checked (exists, exact parameter list, and `_RunAgentDisplay` is a dataclass
with `progress_mode`, `tool_progress_enabled`, `interim_assistant_messages_enabled`, `_thinking_enabled`,
`_native_slack_task_cards`, `needs_progress_queue`, `user_config`). A failed check leaves the core method
untouched and logs ONE WARNING per seam naming the lost behavior:

```
thread-only-display: GatewayTurnMixin._run_agent_display_settings NOT installed: <reason>. Lost behavior: ...
```

Following upstream: `scripts/test-plugin.sh thread-only-display` against the new `hermes-ref.env`.

- `_run_agent_display_settings` renamed/re-signed or `_RunAgentDisplay` fields changed:
  `test_wraps_are_on_the_expected_private_seams`, `test_parent_channel_*`, `test_log_mode_*` fail.
- `_hmwa_prepend_reasoning` renamed/re-signed: `test_wraps_are_on_the_expected_private_seams`,
  `test_prepended_reasoning_is_thread_only` fail.
- `pre_gateway_dispatch` deferral: `test_install_is_deferred_until_gateway_loaded`.
- WARNING path: `test_renamed_seams_warn_once_and_leave_core_untouched`,
  `test_display_dataclass_change_warns_and_unwraps`.

The tests reuse the tree's `tests/gateway/test_run_progress_topics.py` helpers (`_make_runner`,
`_run_with_agent`, `ProgressCaptureAdapter`). While the pinned tree still contains the fork's core
implementation, each test replaces `gateway.display_config.is_thread_only_display_channel` with a
constant False (`test_core_is_neutralized` / `test_core_reasoning_is_neutralized` prove it), so only the
plugin can suppress output.
