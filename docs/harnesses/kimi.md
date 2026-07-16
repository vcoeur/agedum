---
title: kimi harness · agedum
description: How agedum drives Kimi Code — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md bound at ~/.kimi-code/AGENTS.md, skills binds) and the provider config that becomes a generated config.toml plus appended CLI flags.
---

# kimi

Kimi Code reads the **project** `AGENTS.md` from the filesystem natively, and also reads a
**user-scope `AGENTS.md`** at `~/.kimi-code/AGENTS.md`. So agedum leaves the project
instructions in place and binds only the global ones. Skills, both scopes, are injected as
binds like [Claude's](claude.md).

## Wrapper resolution { #wrapper-resolution }

**Project instructions** — Kimi Code merges every `AGENTS.md` from the project root (the
nearest `.git`) down to the working directory into its system prompt (`KIMI_AGENTS_MD`). The
agent-neutral source's `AGENTS.md` already sits at the project root, which is exactly where
Kimi looks, so **agedum injects nothing** for it — and never tries to, since that root
`AGENTS.md` is typically git-tracked.

**Global instructions** — Kimi Code also reads a user-scope `AGENTS.md` at
`~/.kimi-code/AGENTS.md`, so the global `AGENTS.md` (base merged with an optional
`AGENTS.kimi.md`
[overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope)) is bound there:

| Source | Injected at |
|---|---|
| global `~/.config/agents/AGENTS.md` | `~/.kimi-code/AGENTS.md` |

Both scopes merge natively into `KIMI_AGENTS_MD` — the project `AGENTS.md` by tree discovery,
the global one from the user-scope path — so agedum appends **no flag** for instructions. A
project with no global scope needs no injection at all: its `AGENTS.md` is read natively. This
mirrors the Claude harness — each scope kept distinct, never merged.

**Skills** — bound into the directories Kimi Code reads automatically:

| Source | Injected at |
|---|---|
| global `~/.config/agents/skills/` | `~/.kimi-code/skills/` |
| project `.agents/skills/` | `<root>/.kimi-code/skills/` |

- Skills use the `SKILL.kimi.md` overlay where present; assets are copied verbatim.
- The AGENTS.md and skills binds land at paths Kimi Code already reads, so there is **no
  config rewrite** and `extra_args` stays empty.

```bash
agedum --wrapper kimi -- kimi -p "explain this code"
```

## Provider config { #provider-config }

Kimi Code reads its API token **from the environment**, so the key goes in `requiredEnv` (or
`secretEnv`, which is appended automatically) and is exported into the child unchanged. The
`config` knobs become **appended CLI flags** on the launched command:

```json
{
  "harness": "kimi",
  "slug": "kimi",
  "secretEnv": "MOONSHOT_API_KEY",
  "requiredEnv": ["MOONSHOT_API_KEY"],
  "config": { "model": "kimi-k2", "yolo": true }
}
```

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `plan` | `--plan` (true) |
| `yolo` | `--yolo` (true) |
| `binary` | overrides the launched CLI name (default `kimi`) |

The config above launches `kimi --model kimi-k2 --yolo`, with `MOONSHOT_API_KEY` in the
environment. `--run` seeds a one-shot task with `--prompt "<text>"` (Kimi Code's `--prompt`
runs once and exits) — it drops `--yolo`/`--plan`, which Kimi Code refuses to combine with
`--prompt`. `--prompt` (seed-then-stay interactive) is unsupported and fails loudly — see the
[prompt-seeding table](../provider.md#prompt-seeding).

> Kimi Code dropped the `--thinking` / `--no-thinking` flags; thinking is now a config
> setting, so it is applied only through the generated `config.toml` below (which needs
> `baseUrl`).

### Custom endpoint — `baseUrl` { #custom-endpoint }

Kimi Code has **no base-URL flag**, its config does **not** interpolate `$ENV` (so a key can't
be referenced by name the way pi's `models.json` does), and there is **no `--config-file`**
flag. To run Kimi Code against an arbitrary OpenAI-/Anthropic-compatible endpoint, set
`baseUrl`: agedum then **generates a `config.toml`** with one provider (named `agedum`) and one
model, bakes the resolved key into it (masked in `--dry-run`, like opencode's
`OPENCODE_CONFIG_CONTENT`), and binds it over `~/.kimi-code/config.toml` — the file Kimi reads
from its data dir. Because the bind replaces that file inside the namespace, the generated doc
is self-sufficient; Kimi fills every other setting from its own defaults.

```json
{
  "harness": "kimi",
  "secretEnv": "OPENCODE_GO_API_KEY",
  "config": {
    "baseUrl": "https://opencode.ai/zen/go/v1",
    "providerType": "openai",
    "model": "kimi-k2.7-code",
    "contextWindow": 262144,
    "thinking": true
  }
}
```

| `config` key | Generated `config.toml` field | Default |
|---|---|---|
| `baseUrl` | `providers.agedum.base_url` (turns this mode on) | — |
| `providerType` | `providers.agedum.type` | `openai` |
| `model` | `models.<model>.model` + `default_model` (required) | — |
| `contextWindow` | `models.<model>.max_context_size` | `262144` |
| `capabilities` | `models.<model>.capabilities` | `["thinking"]` |
| `thinking` | `[thinking].enabled` (only when set) | — |
| `effortLevel` | `[thinking].effort` (only when set) | — |
| `supportEfforts` | `models.<model>.support_efforts` | — |
| `defaultEffort` | `models.<model>.default_effort` | — |
| (`secretEnv` value) | `providers.agedum.api_key` (resolved key, baked in) | — |

The above launches `kimi --model kimi-k2.7-code`, reading the generated
`~/.kimi-code/config.toml`. `baseUrl` requires `model` + `secretEnv`. `providerType` must name
a Kimi Code provider type (`openai` for an OpenAI Chat Completions surface, `anthropic`,
`kimi`, `google-genai`, `openai_responses`, `vertexai`).

## Thinking effort { #thinking-effort }

`effortLevel` sets `[thinking] effort`, but on the **kimi wire protocol** (`providerType:
"kimi"`) Kimi Code resolves that value against the model's `support_efforts` list, and the
failure modes are both silent-ish:

- **no `supportEfforts`** → Kimi normalises the effort away to plain `on`, so the configured
  effort is discarded while the config still reads as set;
- **an effort outside `supportEfforts`** → Kimi raises `MODEL_CONFIG_INVALID` at launch.

So agedum **requires `supportEfforts` whenever `effortLevel` is set on `providerType: "kimi"`**,
and rejects an `effortLevel` the list doesn't contain — a config that would no-op is an error,
not a surprise at runtime. A model's own roster entry reports these under `think_efforts`
(`valid_efforts` / `default_effort`) in `GET /models`; mirror them:

```json
{
  "config": {
    "providerType": "kimi",
    "model": "k3",
    "contextWindow": 1048576,
    "thinking": true,
    "effortLevel": "max",
    "supportEfforts": ["max"],
    "defaultEffort": "max"
  }
}
```

Widening `supportEfforts` is the seam for later: when a model accepts more efforts, list them
and `effortLevel` can move off `max`. On a non-kimi `providerType` the guard does not apply —
a compatible backend receives the effort string unchanged and makes its own decision.

## MCP servers { #mcp }

Kimi Code reads MCP servers from **`mcp.json`, never `config.toml`**, so `mcpServers` becomes a
second generated doc bound at `~/.kimi-code/mcp.json`. It is bound read-only rather than merged,
so the launcher declares its own server set instead of inheriting the host's:

```json
{
  "config": {
    "mcpServers": {
      "context7": { "command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"] },
      "playwright": { "command": "npx", "args": ["-y", "@playwright/mcp@latest"] }
    }
  }
}
```

Entries use Kimi's MCP shape: stdio takes `command` (+ `args`, `env`, `cwd`); HTTP takes `url`
(+ `bearerTokenEnvVar` for a static token from the environment). `mcpServers` is independent of
`baseUrl` — a launcher can inject MCP without generating a `config.toml`. Kimi also reads a
project-root `.mcp.json` (Claude-compatible) on its own; agedum does not touch that file.
