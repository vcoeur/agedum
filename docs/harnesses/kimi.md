---
title: kimi harness · agedum
description: How agedum drives kimi — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md via a generated --agent-file, skills binds) and the provider config that becomes appended CLI flags.
---

# kimi

kimi reads the **project** `AGENTS.md` from the filesystem natively, but has **no
user-scope `AGENTS.md`** — so agedum leaves the project instructions in place and injects
only the global ones, via a flag. Skills, both scopes, are injected as binds like
[Claude's](claude.md).

## Wrapper resolution { #wrapper-resolution }

**Project instructions** — kimi merges every `AGENTS.md` from the project root (the
nearest `.git`) down to the working directory into its system prompt. The agent-neutral
source's `AGENTS.md` already sits at the project root, which is exactly where kimi looks,
so **agedum injects nothing** for it — and never tries to, since that root `AGENTS.md` is
typically git-tracked.

**Global instructions** — because kimi has no user-scope `AGENTS.md`, the global
`AGENTS.md` (base merged with an optional `AGENTS.kimi.md`
[overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope)) is injected
via a custom agent-file appended to your command:

```text
… kimi -p "…"  --agent-file /tmp/agedum-kimi-XXXX/agent.yaml
```

The generated `agent.yaml` extends kimi's default agent and injects the **global**
instructions as `system_prompt_args.ROLE_ADDITIONAL`:

```yaml
version: 1
agent:
  extend: default
  system_prompt_args:
    ROLE_ADDITIONAL: |
      <global AGENTS.md>
```

This coexists with native discovery: the default agent's system prompt fills
`ROLE_ADDITIONAL` from the agent-file (global) and a separate slot from the merged project
`AGENTS.md` (native) — so both scopes apply, each by the right mechanism.

**Skills** — bound into the directories kimi reads automatically:

| Source | Injected at |
|---|---|
| global `~/.config/agents/skills/` | `~/.kimi/skills/` |
| project `.agents/skills/` | `<root>/.kimi/skills/` |

- Skills use the `SKILL.kimi.md` overlay where present; assets are copied verbatim.
- The project-local `./.kimi/skills/` bind matches the layout kimi already auto-reads, so
  there is **no config rewrite** — `extra_args` carries only `--agent-file` (and only when
  a global `AGENTS.md` exists), never a `--config` override.
- A project with no global scope needs no `--agent-file` at all: its `AGENTS.md` is read
  natively. This mirrors the Claude harness — each scope kept distinct, never merged.

```bash
agedum --wrapper kimi -- kimi -p "explain this code"
```

## Provider config { #provider-config }

kimi reads its API token **from the environment**, so the key goes in `requiredEnv` (or
`secretEnv`, which is appended automatically) and is exported into the child unchanged. The
`config` knobs become **appended CLI flags** on the launched command:

```json
{
  "harness": "kimi",
  "slug": "kimi",
  "secretEnv": "MOONSHOT_API_KEY",
  "requiredEnv": ["MOONSHOT_API_KEY"],
  "config": { "model": "kimi-k2", "thinking": true }
}
```

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `thinking` | `--thinking` (true) / `--no-thinking` (false) |
| `plan` | `--plan` (true) |
| `yolo` | `--yolo` (true) |
| `configInline` | `--config <value>` |
| `binary` | overrides the launched CLI name (default `kimi`) |

The config above launches `kimi --model kimi-k2 --thinking`, with `MOONSHOT_API_KEY` in the
environment. `--prompt`/`--run` add `--prompt "<text>"` (and `--print` for `--run`) — see
the [prompt-seeding table](../provider.md#prompt-seeding).

### Custom endpoint — `baseUrl` { #custom-endpoint }

kimi has **no base-URL flag**, and its config does **not** interpolate `$ENV` (so a key can't
be referenced by name the way pi's `models.json` does). To run kimi against an arbitrary
OpenAI-/Anthropic-compatible endpoint, set `baseUrl`: agedum then **generates a kimi config
file** with one provider (named `agedum`) and one model, bakes the resolved key into it (masked
in `--dry-run`, like opencode's `OPENCODE_CONFIG_CONTENT`), binds it into the namespace, and
loads it with `--config-file`. Because `--config-file` *replaces* the default config, the
generated doc is self-sufficient; kimi fills every other setting from its own defaults.

```json
{
  "harness": "kimi",
  "secretEnv": "OPENCODE_GO_API_KEY",
  "config": {
    "baseUrl": "https://opencode.ai/zen/go/v1",
    "providerType": "openai_legacy",
    "model": "kimi-k2.7-code",
    "contextWindow": 262144,
    "thinking": true
  }
}
```

| `config` key | Generated config field | Default |
|---|---|---|
| `baseUrl` | `providers.agedum.base_url` (turns this mode on) | — |
| `providerType` | `providers.agedum.type` | `openai_legacy` |
| `model` | `models.<model>.model` + `default_model` (required) | — |
| `contextWindow` | `models.<model>.max_context_size` | `262144` |
| `capabilities` | `models.<model>.capabilities` | `["thinking"]` |
| (`secretEnv` value) | `providers.agedum.api_key` (resolved key, baked in) | — |

The above launches `kimi --config-file ~/.kimi/agedum-config.json --model kimi-k2.7-code
--thinking`. `baseUrl` requires `model` + `secretEnv` and is mutually exclusive with
`configInline`. `providerType` accepts any kimi provider type (`openai_legacy` for an OpenAI
Chat Completions surface, `anthropic`, `kimi`, …).
