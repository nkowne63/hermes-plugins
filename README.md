# hermes-plugins

Hermes Agent plugins maintained for the rpi deployment of the
[nkowne63/hermes-agent](https://github.com/nkowne63/hermes-agent) fork. Everything that can live outside the
Hermes core lives here, so the fork itself only carries changes the plugin API cannot express.

## Layout

| Path | What |
|---|---|
| `plugins/<name>/` | One plugin per directory (`plugin.yaml`, `__init__.py`, `tests/`, optional `requirements-dev.txt`) |
| `hermes-ref.env` | The Hermes tree (repo + commit) every plugin is tested against: the fork main the rpi runs |
| `packs/rpi.yaml` | Hermes plugin pack for the rpi: each plugin pinned to an exact commit of this repo |
| `scripts/test-plugin.sh <name>` | Runs a plugin's tests against `hermes-ref.env` (same as CI) |
| `scripts/install-pack.sh <pack>` | Non-interactive pinned install/update of every plugin in a pack |

## Plugins

| Plugin | What | Config keys | Private Hermes API it depends on |
|---|---|---|---|
| `session-reset-policy` | Idle/daily session reset (vendored fork of fastfinge's), per channel/guild, reset notice | `session_reset.*` | — |
| `always-loaded-skills` | Skills injected into every agent incl. delegate children | `skills.always_loaded` | — (llm_request middleware) |
| `channel-silence` | Per-channel "silence allowed" | `<platform>.channel_overrides.<id>.allow_silence` | `BasePlatformAdapter._drop_unresolved` |
| `thread-only-display` | Progress/thinking only inside threads for listed channels | `display.thread_only_channels` | `_run_agent_display_settings`, `_hmwa_prepend_reasoning` |
| `discord-extensions` | Discord guild/category/channel defaults; auto-thread starter reply references | `discord.{guild,category,channel}_defaults` | Discord adapter internals (see its README) |
| `devin` | Devin (Cognition) model provider | credentials via `hermes auth add devin` | needs fork commits for PKCE without client_id and the async-client seam |
| `delegation-no-stall` | `delegate_task` children are never stopped for "no progress" | — | `tools.delegate_tool._HEARTBEAT_STALE_CYCLES_*`, `tools.async_delegation._STALE_*_SECONDS`, `DurableTurnLease.build_threads` |
| `subagent-messaging` | Children-only tools `subagent_peers` / `subagent_send` / `subagent_wait`: message the parent (new parent turn) or siblings without finishing | `plugins.entries.subagent-messaging.{allow_gateway_injection, settings.max_parent_messages_per_child}` | subagent registry internals, `tools.tool_search.is_deferrable_tool_name`, `tools.async_delegation._records` (see its README) |

Every private dependency is checked at startup (one WARNING naming the lost behaviour) and by a test that fails
when upstream changes it. Still in the fork because no plugin seam exists: per-channel `reasoning_effort`, the
Astra model-cache fix, and the two core seams `devin` needs.

## Install

```bash
hermes plugins install https://github.com/nkowne63/hermes-plugins.git#plugins/<name> --ref <commit> --enable
scripts/install-pack.sh packs/rpi.yaml          # all of the rpi set, non-interactive
hermes plugins pack install packs/rpi.yaml      # same, interactive review
```

Restart the gateway after installing or updating.

## Following upstream

- **Hermes releases**: after the fork main moves to a new upstream tag, set `HERMES_REF` / `HERMES_BASE_TAG` in
  `hermes-ref.env`, run `scripts/test-plugin.sh` for every plugin (CI does it on push), fix what broke, then bump
  the commits in `packs/rpi.yaml`.
- **Plugins forked from another repo** are vendored with `git subtree`, with their history, so the original can
  still be merged:

  | Plugin | Original | Pull it |
  |---|---|---|
  | `session-reset-policy` | [fastfinge/hermes-session-reset-policy](https://github.com/fastfinge/hermes-session-reset-policy) | `git subtree pull --prefix plugins/session-reset-policy https://github.com/fastfinge/hermes-session-reset-policy.git main` |

  Keep local changes to a vendored plugin in small commits touching only its directory, so a pull conflicts
  only where the original changed the same lines.
