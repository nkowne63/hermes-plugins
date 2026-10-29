# devin — Devin (Cognition) model provider

The models your Devin account serves (SWE-1.x plus the frontier models on your plan), reached the way the Devin
CLI reaches them: Codeium Cascade's Connect/protobuf API at `https://server.codeium.com`, not an OpenAI-compatible
endpoint. Hermes speaks that wire itself — no subprocess.

Provider `devin` (aliases `cognition`, `devin-cli`). A `kind: model-provider` plugin: once installed under
`$HERMES_HOME/plugins/devin/`, provider discovery registers it (no `register(ctx)`, nothing else to enable).

## Install

```bash
hermes plugins install https://github.com/nkowne63/hermes-plugins.git#plugins/devin --ref <commit>
```

## Use

```bash
hermes auth add devin                 # browser sign-in (Devin CLI login, http://127.0.0.1:59653/callback)
hermes auth add devin --type api-key  # or paste a Devin / Windsurf API key (same as DEVIN_API_KEY)
hermes chat --provider devin --model swe-1-6
```

The model list comes live from your account (`hermes model` → Devin); `adaptive` is Devin's server-side router
and is resolved to a concrete model per turn. The sign-in token has no refresh grant: when it expires, run
`hermes auth add devin` again.

| Variable | Meaning |
|---|---|
| `DEVIN_API_KEY` | Devin (Cognition) or Windsurf API key. Optional: `hermes auth add devin` signs in through the browser instead. |
| `DEVIN_BASE_URL` | Override the Devin API server (default: `https://server.codeium.com`) |

## Hermes core it needs

Two seams live in the Hermes core (the nkowne63/hermes-agent fork); on a Hermes without them:

| Fork commit | Seam | Without it |
|---|---|---|
| `577c293229` feat(auth): PKCE plugin login sends no client_id when the config declares none | `hermes_cli/auth_oauth_pkce_plugin.login` | the browser sign-in's authorize URL carries an empty `client_id=` |
| `e36881c1e7` feat(aux): a provider-supplied client may name its own async twin | `agent/auxiliary_client._to_async_client` | async auxiliary calls (rebuilt as `AsyncOpenAI` from the base URL) speak OpenAI JSON to the Cascade server and fail |

The main chat path, model listing and API-key auth need no core change. Only the default in-process plugin
isolation is exercised: under `plugins.isolation: host` the profile's methods run in the plugin host, and
`create_client()` returns a live client object, which that wire is not built to carry.

## Tests

`scripts/test-plugin.sh devin` (offline: a fake Connect server over `httpx.MockTransport`). The suite installs the
plugin into a scratch `$HERMES_HOME/plugins/devin` and loads it through the user-plugin discovery path. The two
tests of the core seams above xfail, naming the commit, on a Hermes tree that lacks them.
