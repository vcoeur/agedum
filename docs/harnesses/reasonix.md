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
| global `~/.config/agents/skills/` | `~/.reasonix/skills/` |

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
subcommand, and the API token reaches it through the selected provider's `api_key_env`. There are
two shapes, depending on whether you target a provider reasonix already knows or a custom endpoint.

### Built-in / pre-configured provider (no `baseUrl`)

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
| `model` | `--model <name>` — selects a reasonix provider **by name** (a built-in like `deepseek-flash` / `deepseek-pro` / `mimo-pro`, or one configured in `reasonix.toml`) |
| `secretEnv` value | exported into the child (`requiredEnv`); reasonix reads it via the selected provider's `api_key_env` |

This launches `reasonix chat --model deepseek-pro` (interactive) with `DEEPSEEK_API_KEY` in the
environment. A config with no `model` runs bare `reasonix chat`, using the config's
`default_model`. The token is masked in `--dry-run`.

### Custom endpoint (`baseUrl`) { #custom-endpoint }

reasonix has no base-URL flag or environment variable — a custom endpoint is a `[[providers]]`
block in a `reasonix.toml`. So when a config sets `baseUrl`, agedum **generates a minimal
`reasonix.toml`** and injects it at the project root (reasonix's highest-priority TOML source):

```json
{
  "harness": "reasonix",
  "slug": "reasonix-openrouter",
  "secretEnv": "OPENROUTER_API_KEY",
  "config": {
    "baseUrl": "https://openrouter.ai/api/v1",
    "model": "deepseek/deepseek-chat",
    "kind": "openai"
  }
}
```

| `config` key | Effect |
|---|---|
| `baseUrl` | the endpoint → the provider's `base_url`; its presence switches on toml generation |
| `model` | the **upstream model id** at that endpoint → the provider's `model` (here it is *not* a provider name). Required when `baseUrl` is set |
| `kind` | the reasonix provider kind → `kind` (default `openai`) |
| `secretEnv` | the provider's `api_key_env` (omitted for a keyless endpoint) |

The generated file (shown by `--dry-run`) is:

```toml
default_model = "agedum"

[[providers]]
name = "agedum"
kind = "openai"
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-chat"
api_key_env = "OPENROUTER_API_KEY"
```

agedum names the provider `agedum` and selects it with `--model agedum`, so the launch is
`reasonix chat --model agedum`. **The key never lands on disk** — the toml references it by
env-var *name* (`api_key_env`); reasonix reads the value from the exported environment at runtime.

**Why the project root, and what it preserves.** reasonix merges config sources defaults →
`~/.config/reasonix/config.toml` → `./reasonix.toml`. `[[providers]]` is replaced wholesale by the
highest-priority source, but **scalars (theme, language, …) and `[[plugins]]`/MCP from the user
config survive the merge**. Binding at `./reasonix.toml` therefore swaps in agedum's provider for
the session while leaving the user's other settings intact — binding the *user* config would have
masked all of it. The bind goes through the normal launcher path, so it is **refused over a
git-tracked `reasonix.toml`** (an injected provider must not be committable); keep `reasonix.toml`
gitignored in repos you drive this way.

### Two-model routing + multiple providers { #two-model }

reasonix can run a strong **executor** alongside a fast model for subagent skills or planning. Those
knobs live in the generated toml's `[agent]` section (reasonix has no flags for them), and a config
that sets any of them — or `providerDef` — also gets a generated `reasonix.toml`:

| `config` key | reasonix.toml |
|---|---|
| `subagentModel` | `[agent] subagent_model` — default model for `runAs=subagent` skills |
| `plannerModel` | `[agent] planner_model` — planner/executor two-model collaboration |
| `autoPlan` | `[agent] auto_plan` (`off` \| `ask` \| `on`) |
| `providerDef` | one or more `[[providers]]` blocks (`{id, kind, baseUrl, model, apiKeyEnv}`; `kind` default `openai`). A single object or a **list** |

`model` / `subagentModel` / `plannerModel` reference providers **by name** — a built-in
(`deepseek-pro`, `deepseek-flash`, `mimo-pro`, …), a `providerDef` `id`, or one in the user's
`reasonix.toml`. When every referenced provider is a built-in, **omit `providerDef`** — the toml then
carries only `default_model` + `[agent]`, no `[[providers]]`, so reasonix's built-ins survive the
merge:

```json
{ "harness": "reasonix", "slug": "reasonix-deepseek-flash", "secretEnv": "DEEPSEEK_API_KEY",
  "config": { "model": "deepseek-pro", "subagentModel": "deepseek-flash" } }
```

For providers reasonix doesn't ship, define them inline with `providerDef` (each `apiKeyEnv` is
auto-added to `requiredEnv` and exported; the keys are referenced by name, never written). E.g. a Kimi
executor (Anthropic-style endpoint) with DeepSeek-flash subagents:

```json
{ "harness": "reasonix", "slug": "reasonix-kimi-flash",
  "requiredEnv": ["KIMI_API_KEY", "DEEPSEEK_API_KEY"],
  "config": {
    "model": "kimi", "subagentModel": "deepseek-flash",
    "providerDef": [
      { "id": "kimi", "kind": "anthropic", "baseUrl": "https://api.kimi.com/coding", "model": "k2p6", "apiKeyEnv": "KIMI_API_KEY" },
      { "id": "deepseek-flash", "kind": "openai", "baseUrl": "https://api.deepseek.com", "model": "deepseek-v4-flash", "apiKeyEnv": "DEEPSEEK_API_KEY" }
    ] } }
```

→ `default_model = "kimi"`, `[agent] subagent_model = "deepseek-flash"`, and two `[[providers]]`
blocks. (`kind = "anthropic"` speaks the Messages API: reasonix POSTs to `base_url + /v1/messages`, so
a coding endpoint mounted at `…/coding/v1/messages` uses `base_url = "…/coding"`.) `baseUrl` and
`providerDef` are mutually exclusive — `baseUrl` is the single-inline shorthand, `providerDef` the
explicit form.

**`--prompt`/`--run`.** reasonix's `run` subcommand takes the task as a positional argument and
exits, but `chat` cannot be pre-seeded. So `agedum reasonix-<name> --run "<text>"` maps to
`reasonix run "<text>"` (the base `chat` subcommand becomes `run`, `--model` preserved), while
`--prompt` — which must stay interactive — is a fail-loud `ProviderError`. condash then falls back
to spawn-and-type for an interactive seed. See the
[prompt-seeding table](../provider.md#prompt-seeding).
