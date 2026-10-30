# delegation-no-stall

`delegate_task` children are never stopped because their progress signals (API-call count, current tool,
activity clock) stopped moving for a while. A child that waits on a long build, a slow external service or
another agent keeps running until it finishes, is stopped explicitly, or the gateway shuts down.

No configuration: enabling the plugin is the switch.

## What it switches off

| Core check | Default | Plugin |
|---|---|---|
| Per-child heartbeat stale verdict (`tools/delegate_tool_child_run.py` `_Heartbeat.tick`): abandons the child's wait | 450 s idle / 1200 s on one tool | never (`tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IDLE` / `_IN_TOOL` = `sys.maxsize`) |
| Background-delegation monitor (`tools/async_delegation.py` `_sweep_stale_locked`): interrupts, then force-finalizes as `stalled` | 450 s idle / 1200 s on one tool | never (`_STALE_IDLE_SECONDS` / `_STALE_IN_TOOL_SECONDS` = `inf`) |
| Turn-liveness watchdog (`agent/turn_liveness.py`, `agent.turn_liveness.timeout_s`): aborts a turn whose activity clock froze | 600 s | removed for child turns only (`DurableTurnLease.build_threads` wrapped; `_delegate_depth > 0`) |

The heartbeat keeps running: it still refreshes the parent's activity clock, which is what keeps the gateway
inactivity timeout (`agent.gateway_timeout`) from firing while a parent waits on children synchronously.

## What still stops a child

- `delegate_task(action="stop", subagent_id=…)`, `/stop`, `/new`, a session reset, gateway shutdown.
- `delegation.child_timeout_seconds` when configured (an explicit inactivity cap; off by default).
- `delegation.max_iterations`.
- The per-API-call stale watchdog (`providers.*.stale_timeout_seconds`) still kills one hung model call; the
  child sees an error and retries, it is not stopped.

A child wedged inside a tool that never returns therefore holds its delegation slot
(`delegation.max_concurrent_children`) until one of the explicit stops above.

## Private dependencies

All three seams are private core names. At `register()` the plugin checks each one; a missing or changed seam
is logged once and the others still apply:

```
delegation-no-stall: tools.async_delegation._STALE_IDLE_SECONDS is missing, so background delegations idle
for 450 s are still stopped as stalled. Update the plugin for this Hermes version.
```

The plugin needs `plugins.isolation: in_process` (the default).

## Following upstream

`scripts/test-plugin.sh delegation-no-stall` against the new `hermes-ref.env`:

- `test_core_*` fail when upstream changed or removed a check itself (re-read it before adapting the plugin).
- `test_heartbeat_never_abandons_a_child_frozen_for_a_day`, `test_monitor_never_stalls_…`,
  `test_child_turns_get_no_liveness_watchdog` fail when a seam moved (renamed global, no longer read at call
  time, `build_threads` re-signed or no longer where the watchdog is created).
