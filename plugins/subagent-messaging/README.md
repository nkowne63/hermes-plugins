# subagent-messaging

Running `delegate_task` subagents can message their parent and each other without finishing. The parent
already reaches a running child with the stock `delegate_task(action="steer", subagent_id=…, message=…)`; this
plugin adds the other directions.

## Tools (delegate_task children only)

| Tool | What |
|---|---|
| `subagent_peers` | The caller's `subagent_id`, and its running siblings (id, task, status, last tool) |
| `subagent_send(to, message)` | `to` = `"parent"`, a sibling's `subagent_id`, or `"siblings"` (every running sibling) |
| `subagent_wait(timeout_seconds)` | Blocks until a message for the caller is queued (`message_pending`) or the timeout passes (default 300 s, max 1800 s) |

The tools are registered with a children-only `check_fn` (`agent.delegation_context.is_delegated_child_context`,
uncached), so top-level agents never see them, and they are never deferred behind `tool_search`.

Siblings = children of the same parent conversation (same parent subagent, same owning session id), including
children of separate `delegate_task` calls. A child cannot address another conversation's children.

## Delivery

- **To a sibling**: steered into the sibling's running turn (`tools.delegate_tool_registry.steer_subagent`); it
  appears after the sibling's current step as an out-of-band message starting with
  `[SUBAGENT MESSAGE from sibling <id> (task: …)]`. A sibling that already finished is reported as not running.
- **To the parent**:
  - top-level parent: messages to one parent conversation within 2 s are joined into ONE new turn of the parent's
    chat session (`ctx.inject_message(session_key=…)`; the gateway commits it to the session's durable FIFO, so a
    busy parent gets it right after its current turn). The text names each sender, its task and the
    `delegate_task(action="steer", …)` call that answers it. The parent's reply goes to the chat as usual.
  - when injection is refused or there is no gateway route (CLI, one-shot runs): steered into the live parent
    agent (lands in its current or next turn).
  - parent that is itself a subagent (orchestrator, `delegation.max_spawn_depth >= 2`): steered into it.
- `subagent_wait` returns as soon as anything is queued for the caller — a sibling message or a parent steer —
  and the message is delivered right after the tool result. While waiting it refreshes the caller's activity
  clock every 15 s.

## Config

```yaml
plugins:
  enabled: [subagent-messaging]
  entries:
    subagent-messaging:
      allow_gateway_injection: true        # required for parent messages to start a turn in a chat session
      settings:
        max_parent_messages_per_child: 20  # default 20; further sends tell the child to use its final result
```

Without `allow_gateway_injection`, Hermes logs `inject_message: gateway injection denied for plugin
subagent-messaging` and the plugin falls back to steering the parent agent, which an idle parent only reads on its
next turn.

## Private dependencies

Checked at `register()`; each unusable seam is logged once:

| Seam | Used for | When missing |
|---|---|---|
| `tools.delegate_tool_registry._active_subagents`, `_active_subagents_lock`, `steer_subagent(subagent_id, text, …)` | caller/sibling lookup, delivery | no tools are registered |
| `tools.registry.no_cache_check_fn`, `agent.delegation_context.is_delegated_child_context` | children-only tools | no tools are registered |
| `tools.tool_search.is_deferrable_tool_name(name, defer_tools)` (wrapped) | tools never deferred | tools may sit behind `tool_search` |
| `tools.async_delegation._records` / `_records_lock` | parent route when the parent agent object was rebuilt | parent reachable only while its agent is in memory |
| `AIAgent._pending_steer` / `_pending_steer_lock`, `_touch_activity` | `subagent_wait` | read with `getattr`; a renamed slot makes `subagent_wait` run to its timeout |

The plugin needs `plugins.isolation: in_process` (the default).

## Following upstream

`scripts/test-plugin.sh subagent-messaging` against the new `hermes-ref.env` (real plugin discovery, real
`AIAgent` parents and `_build_child_agent` children, tool calls through `model_tools.handle_function_call`):

- registry / steer seam changed: the sibling, broadcast, peers and orchestrator tests fail, or
  `test_missing_registry_seam_warns_and_registers_no_tools` shows the warning path.
- children-only check or tool-search wrap broken: `test_only_delegate_children_get_the_tools`,
  `test_tools_stay_direct_when_tool_search_defers_plugin_tools`.
- plugin injection contract changed: `test_parent_message_wakes_the_parent_chat_session`,
  `test_parent_messages_sent_together_arrive_as_one_turn`.
- steer buffer renamed: the `test_wait_*` tests.
