# channel-silence

Per-channel `allow_silence` for the Hermes gateway: a human turn in an opted-in channel (or a thread
under it) may end on a bare silence marker (`[SILENT]`, `NO_REPLY`, …) without the visible ⚠️ fallback
notice. Replaces fork commit `c7d91aef35` (`feat(gateway): per-channel allow_silence override`).

## Config

Same keys as the fork, read from the profile's `config.yaml` (core `ChannelOverride.from_dict`
drops unknown keys, so the plugin reads the file itself, without the `DEFAULT_CONFIG` merge, exactly as
the gateway does):

```yaml
discord:                     # any platform; platforms.<name> / gateway.platforms.<name> work too
  channel_overrides:
    "123456789012345678":
      allow_silence: true    # bool or bool-ish string
```

Lookup is the gateway's own: the first existing override of chat id → thread id → parent id wins, so a
thread inherits its parent unless the thread has its own override (with or without the key). Internal
events (background-process notifications etc.) keep their own verdict. A multiplexed profile reads
its own `profiles/<name>/config.yaml`.

## How it works: two seams

The plugin only stamps `MessageEvent.reply_expected = False`; Hermes' existing
`silence_allowed(display_kind, reply_expected)` path does the rest (no ⚠️, persisted
`reply_expected: false` for crash-recovery replay, `absorb_reply_expected` on merges,
`queued_terminal_reply_expected` for a queued chain's last turn).

1. **Adapter ingress (wrapper)**: `BasePlatformAdapter._drop_unresolved` is wrapped. Every adapter
   ingress (`handle_message`, text batching `_enqueue_text_event`, Telegram's batch lane) runs this
   identity gate first, so the stamp lands before session-key derivation, busy queueing, debounce and
   burst merging, which is where the fork stamped it. This seam is required: a message arriving
   while a turn runs goes to the busy handler / pending slot / runner FIFO and is drained inside the
   running turn, never through `pre_gateway_dispatch`.
2. **`pre_gateway_dispatch` (official hook)**: stamps first turns and producer paths (webhook/relay)
   that reach the runner without an adapter ingress. If seam 1 is unavailable this is the only one.

## Private-method dependency

Seam 1 depends on the private `BasePlatformAdapter._drop_unresolved(self, event) -> bool` (truthy =
dropped). If upstream removes/renames it or changes its signature, the wrapper is not installed and
the plugin logs a WARNING once:

```
channel-silence: adapter ingress stamp NOT installed: <reason>. Only pre_gateway_dispatch stamps now,
so queued follow-ups in allow_silence channels will show the ⚠️ fallback ...
```

First turns stay quiet via seam 2. The plugin needs `plugins.isolation: in_process` (the default): in
the plugin host it cannot wrap the gateway's classes.

## Following upstream

`scripts/test-plugin.sh channel-silence` against the new `hermes-ref.env` detects breakage:

- `_drop_unresolved` gone or re-signed: the busy/queued tests fail
  (`test_busy_follow_up_is_stamped_before_it_is_queued`, `test_busy_session_handler_sees_stamped_event`,
  `test_real_pending_slot_merge_keeps_verdict`, `test_text_batch_merge_keeps_verdict`, and the
  adapter-ingress tests such as `test_human_turn_in_allow_silence_channel_is_unaddressed`).
  `test_unusable_adapter_gate_warns_once_and_hook_still_stamps` covers the warning path itself.
- Gate still there but no longer called before queueing: the busy/queued tests above fail.
- Silence semantics changed in core (`silence_allowed`, persisted metadata):
  `test_discord_human_turn_silence_end_to_end` fails.
