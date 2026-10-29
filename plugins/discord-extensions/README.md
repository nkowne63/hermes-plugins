# discord-extensions

The bundled Discord adapter with two additions. Replaces fork commits `57d2d5b2c2`
(`feat(discord): guild/category/channel defaults for require_mention and thread_response`) and `e8e420baae`
(`fix(discord): reply to an auto-thread starter via its parent channel`).

Both features live in one plugin on purpose: each replaces the `discord` platform registration, and a
second plugin doing the same would silently win over the first (last registration wins).

## How it replaces the bundled adapter

`register()` runs the bundled Discord plugin's own `register()` (`plugins.platforms.discord`) through a
context proxy that swaps only `adapter_factory` (`DiscordAdapter` → `DiscordExtensionsAdapter`, a subclass);
every other `register_platform` field is the bundled one. Bundled platforms register a deferred loader;
a user plugin loads afterwards, so its concrete registration supersedes the loader (the bundled adapter is then
never imported under its `hermes_plugins.platforms__discord` name). `hermes plugins disable` / unload
restores the bundled loader (plugin ledger inverse). Upstream supports this pattern ("a user plugin
overriding" a bundled platform: `gateway/run_startup.py`, `plugins/platforms/discord/gateway_env.py`).

Cost: user plugins load eagerly, so every Hermes process with the plugin enabled imports the Discord
adapter (and discord.py) at plugin discovery instead of on first use. Needs `plugins.isolation: in_process`
(the default).

## Config

Same keys as the fork; untyped keys of the root `discord:` block already reach `PlatformConfig.extra`, so
no config change is needed.

```yaml
discord:
  require_mention: true
  no_thread_channels: []   # channel IDs — and now category IDs: every channel in it replies inline
  guild_defaults:
    "111111111111111111": { require_mention: true, thread_response: true }
  category_defaults:
    "222222222222222222": { require_mention: false, thread_response: true }  # no mention, still threads
  channel_defaults:
    "333333333333333333": { require_mention: true }                          # beats the category rule
```

- Most specific rule wins: `channel_defaults` (the channel, then a thread's parent) > `category_defaults`
  (the channel's category, or a thread parent's) > `guild_defaults` > global `require_mention`. For
  `thread_response`, auto-threading stays on when no rule sets it. IDs match as strings (unquoted YAML
  integers work); values may be bools or bool-ish strings.
- `require_mention: false` lets a scope answer without an `@mention` in the live and recovered/missed-message
  gates, and admits messages that mention someone else (like a free-response channel).
  `free_response_channels`, voice-linked channels and threads the bot joined never need a mention.
- `thread_response: false` replies inline. `thread_response: true` never overrides `no_thread_channels`, a
  free-response channel's inline default, or `auto_thread: false`. `ignored_channels` / `allowed_channels`
  are checked first and still win.

Auto-thread replies: a reply into an auto-thread whose target is the thread starter (same id as the thread)
names the parent channel in its `MessageReference`; forum/media posts and threads with an uncached parent
keep the thread id. Only reply-anchor rejections (`10008`, or `50035` with `In message_reference…` /
"Cannot reply to a system message") retry as a plain send; other 50035s (content length, embeds) fail
without a duplicate request.

## Known gap vs. the fork

The native voice-message payload (`DiscordMediaMixin.send_voice`, built by hand, not via
`MessageReference`) still omits `channel_id`. A voice reply to an auto-thread starter is therefore refused
by Discord and retried as a plain voice message (no reply anchor) through the narrowed
`_is_reply_reference_rejected`; it is not lost. Closing it would mean copying ~80 lines of `send_voice`.

## How the overrides work

Scoped defaults: the three ingress methods are wrapped to set a `ContextVar` holding (message, stage) for
their duration; three helpers return scoped values only while that stage is active:

| Stage (wrapped method) | Helper override |
|---|---|
| `handle` (`_handle_message`), `recovered` (`_dispatch_recovered_message`) | `_discord_require_mention()` → scoped rule, else global |
| `admission` (`_discord_message_admission`) | `_discord_free_response_channels()` + the channel when a scoped `require_mention: false` applies |
| `handle` | `_get_no_thread_channels()` + the channel when its category is listed or a scoped `thread_response: false` applies |

Outside these stages (backfill channel selection, thread free-channel checks, anything else) the helpers
return the plain config. Reply references: `_message_reference_from_ids` and `_is_reply_reference_rejected`
are overridden. The plugin's own helpers are prefixed `_discord_extensions_` so they never collide with
the fork's core names while the fork still carries the commits (both are then active and agree).

## Private-API dependencies

Checked at register time (`check_dependencies`): presence, staticmethod-ness and parameter names. A changed
dependency switches off its feature (pass-through to the bundled behavior) and logs ONE WARNING, e.g.
`discord-extensions: Hermes' Discord adapter changed (DiscordAdapter._handle_message(...) != expected ...);
disabled: discord.{channel,category,guild}_defaults ...`. If the bundled `register()` stops calling
`register_platform(name="discord", adapter_factory=DiscordAdapter)`, a WARNING says the adapter is not
overridden.

| `DiscordAdapter` member | Use | Feature |
|---|---|---|
| `_discord_message_admission(message, *, claim)` | wrapped (scope) | scoped defaults |
| `_dispatch_recovered_message(message)` | wrapped (scope) | scoped defaults |
| `_handle_message(message, role_authorized=False, *, recovered=False)` | wrapped (scope) | scoped defaults |
| `_discord_require_mention()` | overridden; must stay the gate's call in `_handle_message` / `_dispatch_recovered_message` | scoped defaults |
| `_discord_free_response_channels()` | overridden; must stay the admission "mentions someone else" check | scoped defaults |
| `_get_no_thread_channels()` | overridden; must stay the auto-thread skip check | scoped defaults |
| `_get_parent_channel_id(channel)` | called | scoped defaults |
| `_message_reference_from_ids(message_id, channel)` | overridden (static upstream; any call via `self.`) | starter reply reference |
| `_reply_reference_for_send(reply_to, channel)` | must keep calling `self._message_reference_from_ids` | starter reply reference |
| `_is_reply_reference_rejected(err)` (staticmethod) | overridden; called via `self.` by send / send_voice | starter reply reference |
| `_is_forum_parent(channel)` | called | starter reply reference |
| `plugins.platforms.discord.register(ctx)` | run through the proxy | both |

## Following upstream

`scripts/test-plugin.sh discord-extensions` against the new `hermes-ref.env` detects breakage:

- A renamed / re-signed member: `test_every_private_dependency_exists_with_the_expected_signature`
  (`test_changed_dependency_disables_its_feature_with_one_warning` covers the warning itself).
- A member still there but no longer called where the gate is (helper moved, inlined, or a new call path):
  the ported behavior tests fail — `tests/test_scoped_defaults.py` (from the fork's
  `test_discord_scoped_defaults.py`, 18 tests) and `tests/test_auto_thread_reply_reference.py` (from
  `test_discord_auto_thread_reply_reference.py`, all but the voice-payload test).
- Bundled `register()` changed: `test_registration_is_the_bundled_one_with_only_the_adapter_replaced`,
  `test_bundled_register_change_is_reported`.
- Supersede/restore semantics of the plugin manager:
  `test_enabled_plugin_supersedes_bundled_discord_and_unload_restores_it`.

The pinned tree may still contain the fork commits; `tests/conftest.py::neutralize_core` puts the bundled
class back to upstream behavior for every test, and `test_neutralized_bundled_class_*` prove it lacks both
features, so passing tests are the plugin's doing.
