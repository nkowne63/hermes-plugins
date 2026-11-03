# codex-account-failover

When the `openai-codex` primary fails for a reason that is not about the credential, retry the same model on
the other account of the codex credential pool first; `fallback_providers` (another provider and model) only
take over when that account fails too.

No configuration: enabling the plugin is the switch. `model`, `credential_pool_strategies` and
`fallback_providers` stay as they are.

## What changes

Hermes already rotates the pool on credential failures (`rate_limit`, `billing`, `auth`, `model_entitlement`:
`recover_with_credential_pool`, which benches the failed account). Every other failure that ends a primary
attempt reaches `AIAgent._try_activate_fallback` and switched provider straight away. With the plugin, that
call first swaps to another pool account when all of these hold:

| Condition | Detail |
|---|---|
| Provider | the agent is on `openai-codex` and its credential pool belongs to that provider/endpoint |
| Reason | none (empty / malformed / invalid response), `incomplete_response` (reasoning-only stall), `server_error`, `overloaded`, `timeout`, `unknown`, `upstream_blocked` |
| Not a refusal | reason-less calls from the safety-refusal paths (`handle_content_policy_refusal`, `_content_filter_fallback`) pass through: another account gets the same refusal |
| Account | another pool entry is available for this model now (`CredentialPool.reclaim`: cooldowns, model-scoped cooldowns, token refresh) and was not used yet in this turn |

On a swap the call returns True, so the caller retries the same model exactly as it would retry on a fallback
provider (counters reset, system message re-synced, the empty call refunded). The failed account is **not**
benched: other sessions keep using it. When the account the session left outranks the one it moved to,
`_credential_pool_revert_id` is armed (unless already armed) and Hermes' own per-turn
`_revert_credential_rotation` moves the session back at the start of the next turn.

Each account is used at most once per turn (tracked per `AIAgent._current_turn_id`). With the rpi pool
(prolite priority 0, sci priority 1) a turn goes: prolite fails → sci, same model → sci fails →
anthropic opus → sonnet. Excluded reasons (`context_overflow`, `payload_too_large`, `format_error`,
`content_policy_blocked`, `model_not_found`, …) and credential reasons go to `fallback_providers` unchanged.

Log line on a swap (INFO):

```
codex-account-failover: failed response on gpt-6.1-sol via account 52351a (prolite) — retrying the same model
on account ea575a (sci) before fallback_providers; back to the first account next turn
```

## Private dependencies

| Seam | Used for |
|---|---|
| `agent.chat_completion_helpers.try_activate_fallback(agent, reason, reset_at)` | wrapped; what `AIAgent._try_activate_fallback` forwards to on every call (checked when `run_agent` is already imported) |
| `AIAgent._credential_pool`, `_credential_pool_entry_id`, `_credential_pool_revert_id`, `_swap_credential`, `_current_turn_id` | the pool, the failing account, the next-turn revert, the swap, the turn boundary |
| `agent.turn_truncation.handle_content_policy_refusal`, `_content_filter_fallback` | recognising reason-less refusal calls |

At `register()` the plugin checks the wrapped seam and the refusal callers; a problem is logged once:

```
codex-account-failover: agent.chat_completion_helpers.try_activate_fallback is missing, so non-credential
codex failures go straight to fallback_providers without retrying the same model on the other codex account.
Update the plugin for this Hermes version.
```

The plugin needs `plugins.isolation: in_process` (the default).

## Following upstream

`scripts/test-plugin.sh codex-account-failover` against the new `hermes-ref.env`:

- `test_core_empty_response_goes_straight_to_fallback_providers` fails when Hermes itself changed what an empty
  codex response does (re-read the empty/invalid-response path before adapting the plugin).
- `test_empty_response_retries_the_same_model_on_the_other_account`,
  `test_next_turn_reverts_to_the_preferred_account_and_may_fail_over_again` and
  `test_the_agent_method_still_forwards_to_the_wrapped_helper` fail when the seam, the swap or the revert hook
  moved.
- `test_registers_on_this_hermes_without_a_warning` fails when the startup check would warn on the rpi.
