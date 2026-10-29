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

## Install one plugin

```bash
hermes plugins install https://github.com/nkowne63/hermes-plugins.git#plugins/<name> --ref <commit> --enable
```

or the whole set (interactive review): `hermes plugins pack install packs/rpi.yaml`.

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
