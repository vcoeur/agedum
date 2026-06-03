---
title: reasonix harness · agedum
description: How agedum drives reasonix (DeepSeek-Reasonix) — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md to ~/.config/reasonix/AGENTS.md, skills to .reasonix/skills) and the provider config mapped to reasonix's chat/run subcommand, with the API token passed through the provider's api_key_env.
---

# reasonix

[reasonix](https://github.com/esengine/DeepSeek-Reasonix) (DeepSeek-Reasonix) is a
DeepSeek-native terminal coding agent. Like [opencode](opencode.md) and [Cline](cline.md), it
is **pure path-discovery** in wrapper mode — it reads its instructions and skills from fixed
locations and needs no flags. reasonix reads `AGENTS.md` as one of its memory docs (alongside
`REASONIX.md` / `CLAUDE.md`), which is exactly the agent-neutral source, so the project
instructions stay in place.

## Wrapper resolution { #wrapper-resolution }

| Source | Injected at |
|---|---|
| project `AGENTS.md` | *(not injected — read natively at `./AGENTS.md`)* |
| project `.agents/skills/` | `<root>/.reasonix/skills/` |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.reasonix.md` overlay) | `~/.config/reasonix/AGENTS.md` |
| global `~/.agents/skills/` | `~/.reasonix/skills/` |

- **Project instructions** — reasonix discovers memory docs (`REASONIX.md` / `AGENTS.md` /
  `CLAUDE.md`) up the project tree and folds them into its cache-stable system prompt. The
  project-root `AGENTS.md` is the agent-neutral source, already in place, so **agedum injects
  nothing** for it — and never could, since the root `AGENTS.md` is git-tracked.
- **Global instructions** — reasonix reads a user-global memory doc from its config dir
  `~/.config/reasonix/` (`os.UserConfigDir()/reasonix`), so the global `AGENTS.md` is bound to
  `~/.config/reasonix/AGENTS.md` — base merged with an optional `AGENTS.reasonix.md`
  [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope).
- **Skills** — reasonix scans four convention dirs (`.reasonix` / `.agents` / `.agent` /
  `.claude`, each `/skills`) under both the project root and the home dir, highest-priority
  first, and `.reasonix` leads. Each skill is a `SKILL.md` folder (the shape reasonix expects),
  compiled with the `SKILL.reasonix.md` overlay and bound to `./.reasonix/skills/` (project) and
  `~/.reasonix/skills/` (global). Because `.reasonix` outranks `.agents`, the overlaid copy wins
  over the raw `.agents/skills/` reasonix would also discover.
- `extra_args`: **none** — reasonix discovers everything from disk, like Claude, opencode, and
  Cline.

```bash
agedum --wrapper reasonix -- reasonix chat        # drive reasonix with the same source
agedum --wrapper reasonix --dry-run -- reasonix    # show what would be injected
```

## Provider config { #provider-config }

reasonix is **DeepSeek-native**: its provider/model selection is a flag on the `chat`/`run`
subcommand, and the API token reaches it through the selected provider's `api_key_env`:

```json
{
  "harness": "reasonix",
  "slug": "reasonix-deepseek-pro",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "model": "deepseek-pro"
  }
}
```

| `config` key | Effect |
|---|---|
| `model` | `--model <name>` — selects a reasonix provider by name (a built-in like `deepseek-flash` / `deepseek-pro` / `mimo-pro`, or one configured in `reasonix.toml`) |
| `secretEnv` value | exported into the child (`requiredEnv`); reasonix reads it via the selected provider's `api_key_env` |

The config above launches `reasonix chat --model deepseek-pro` (interactive) with
`DEEPSEEK_API_KEY` in the environment. A config with no `model` runs bare `reasonix chat`, using
the config's `default_model`. The token is masked in `--dry-run`.

**No `baseUrl`.** reasonix has no base-URL flag or environment variable — a custom endpoint is a
`[[providers]]` block in `reasonix.toml` / `~/.config/reasonix/config.toml`, selected by name. So
a `baseUrl` in a reasonix config is a fail-loud `ProviderError`, not a silent no-op; `model` must
name a provider reasonix already knows (a built-in, or one you add to its TOML, whose
`api_key_env` your `secretEnv` supplies).

**`--prompt`/`--run`.** reasonix's `run` subcommand takes the task as a positional argument and
exits, but `chat` cannot be pre-seeded. So `agedum reasonix-<name> --run "<text>"` maps to
`reasonix run "<text>"` (the base `chat` subcommand becomes `run`, `--model` preserved), while
`--prompt` — which must stay interactive — is a fail-loud `ProviderError`. condash then falls back
to spawn-and-type for an interactive seed. See the
[prompt-seeding table](../provider.md#prompt-seeding).
