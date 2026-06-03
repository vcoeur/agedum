---
title: opencode harness · agedum
description: How agedum drives opencode — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md and skills bound to ~/.config/opencode) and the provider config translated into an OPENCODE_CONFIG_CONTENT document, with providerDef, per-agent routing, and a transcript-capture plugin.
---

# opencode

opencode is **pure path-discovery** — it reads instructions and skills from fixed
locations and needs no flags — so in wrapper mode every scope is a bind and nothing is
appended to your command. It is the closest harness to [Claude](claude.md); the one
difference is that the project instructions are read in place rather than relocated.

## Wrapper resolution { #wrapper-resolution }

| Source | Injected at |
|---|---|
| project `AGENTS.md` | *(not injected — read natively at `./AGENTS.md`)* |
| project `.agents/skills/` | `<root>/.opencode/skills/` |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.opencode.md` overlay) | `$XDG_CONFIG_HOME/opencode/AGENTS.md` (default `~/.config/opencode/AGENTS.md`) |
| global `~/.config/agents/skills/` | `$XDG_CONFIG_HOME/opencode/skills/` (default `~/.config/opencode/skills/`) |

- **Project instructions** — opencode reads the root `AGENTS.md` (traversing up from the
  work dir) as its project rules file. That is exactly the agent-neutral source, already in
  place, so **agedum injects nothing** for it — and never could, since the root `AGENTS.md`
  is git-tracked.
- **Global instructions** — opencode reads `~/.config/opencode/AGENTS.md` as its user-scope
  rules file, so the global `AGENTS.md` is bound there — base merged with an optional
  `AGENTS.opencode.md`
  [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope).
- **Skills** — compiled with the `SKILL.opencode.md` overlay and bound to
  `./.opencode/skills/` (project) and `~/.config/opencode/skills/` (global). opencode
  searches those directories **before** the project's raw `.agents/skills/` (which it would
  otherwise read directly), so the overlaid copy wins. The global skills source is
  `~/.config/agents/skills/`, delivered only via the bind above.
- `extra_args`: **none** — opencode discovers everything from disk, like Claude.

```bash
agedum --wrapper opencode -- opencode run "review this change"
agedum --wrapper opencode -- opencode            # interactive TUI
```

## Provider config { #provider-config }

opencode resolves provider credentials from its **own** auth store (`opencode auth
login`), so a key is only in `requiredEnv` when opencode itself reads it from the
environment — or when a [`providerDef`](#providerdef) bakes it into the config. The
`config` block is translated into opencode's `OPENCODE_CONFIG_CONTENT` document (a single
env var; no file written):

```json
{
  "harness": "opencode",
  "slug": "opencode-deepseek",
  "requiredEnv": ["DEEPSEEK_API_KEY"],
  "config": {
    "model": "deepseek/deepseek-v4-pro",
    "disableExternalSkills": true,
    "effortLevel": "high",
    "agentOptions": [
      { "agent": "general", "model": "deepseek/deepseek-v4-flash", "reasoningEffort": "low" }
    ]
  }
}
```

| `config` key | Effect |
|---|---|
| `model` | `model` field of `OPENCODE_CONFIG_CONTENT` |
| `disableExternalSkills` | `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` |
| `defaultOptions.{reasoningEffort,textVerbosity,reasoningSummary}` | the default model's `provider.<id>.models.<model>.options` |
| `effortLevel` (flat alias) | the default model's `reasoningEffort` (explicit `defaultOptions.reasoningEffort` wins) |
| `agentOptions[]` | per-agent `agent.<name>` model + options; `primary: true` sets `mode: "primary"` for custom (non-built-in) agents |
| `providerDef` | an explicit provider block with the key resolved from the environment — see [below](#providerdef) |
| `opencodeConfig` | a literal opencode config object, deep-merged last (wins on conflict) — see [below](#opencodeconfig) |
| `emitTranscript` | inject the bundled transcript-capture plugin (default **on**); set `false` to opt out — see [below](#emittranscript) |

### `providerDef` — declare the provider + key inline { #providerdef }

By default an opencode `model` like `openrouter/deepseek/deepseek-v4-pro` relies on
opencode resolving the `openrouter` provider from its **own** auth store. `providerDef`
instead **defines the provider in the config** and resolves the API key from the
environment, so no prior login is needed:

```json
{
  "harness": "opencode",
  "requiredEnv": ["OPENROUTER_API_KEY"],
  "config": {
    "model": "openrouter/deepseek/deepseek-v4-pro",
    "providerDef": {
      "id": "openrouter",
      "npm": "@openrouter/ai-sdk-provider",
      "baseUrl": "https://openrouter.ai/api/v1",
      "apiKeyEnv": "OPENROUTER_API_KEY"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `id` | provider id; must match the prefix of the `model` strings (e.g. `openrouter`) |
| `npm` | the AI-SDK package opencode loads for the provider |
| `baseUrl` | becomes `provider.<id>.options.baseURL` |
| `apiKeyEnv` | env var whose **value** is resolved into `provider.<id>.options.apiKey` |

The key's **value** (not a `{env:…}` placeholder) is written into
`provider.<id>.options.apiKey`, because opencode's `{env:…}` substitution is unreliable for
a custom provider's `options.apiKey`. This is the same in-process token handling `claude`
uses for `ANTHROPIC_AUTH_TOKEN`; `apiKeyEnv` is auto-added to the validated `requiredEnv`,
and the resulting `OPENCODE_CONFIG_CONTENT` is masked in `--dry-run`. (Keys containing `"`
or `\` would break the surrounding JSON; standard `sk-or-…` keys are fine.)

`providerDef` may also be a **list** when one config draws models from more than one
provider — e.g. a Kimi primary model plus DeepSeek fast subagents, each needing its own
baked-in key. Entries apply in order (later deep-merge over earlier), and every entry's
`apiKeyEnv` is auto-added to `requiredEnv`:

```json
{
  "harness": "opencode",
  "requiredEnv": ["KIMI_API_KEY", "DEEPSEEK_API_KEY"],
  "config": {
    "model": "kimi-for-coding/kimi-k2.6",
    "agentOptions": [
      { "agent": "general", "model": "deepseek/deepseek-v4-flash" }
    ],
    "providerDef": [
      { "id": "kimi-for-coding", "npm": "@ai-sdk/anthropic",        "baseUrl": "https://api.kimi.com/coding/v1", "apiKeyEnv": "KIMI_API_KEY" },
      { "id": "deepseek",        "npm": "@ai-sdk/openai-compatible", "baseUrl": "https://api.deepseek.com",        "apiKeyEnv": "DEEPSEEK_API_KEY" }
    ]
  }
}
```

### `emitTranscript` — in-band transcript capture (default on) { #emittranscript }

opencode runs as a full-screen alternate-screen TUI, so a terminal capturer (condash,
`script`, tmux, asciinema) only ever sees the current frame — the conversation that scrolls
inside the TUI is repainted, never retained. agedum **ships and auto-injects** a small
opencode plugin (`agedum/assets/opencode/transcript-osc.js`) that streams each finalized
message into the terminal as a **neutral OSC escape** the terminal ignores for display:

```
ESC ] 7373 ; agent-transcript ; <frameId> ; <i> ; <n> ; <base64piece> BEL
```

A capturer recovers a clean transcript by reassembling the base64 pieces and decoding the
JSON frames (`{v,t:"msg",sid,mid,role,text}` / `{v,t:"end"}`, where `role` is `user`,
`assistant`, or `reasoning`). The protocol **names no viewer**, so agedum stays
viewer-agnostic. The plugin path is appended to `OPENCODE_CONFIG_CONTENT.plugin` (unioned
with any `opencodeConfig.plugin`); agedum's bwrap launch binds the whole filesystem, so the
bundled path resolves inside the namespace. Set `"emitTranscript": false` to disable.

### `opencodeConfig` — anything agedum doesn't model { #opencodeconfig }

The keys above are the common, cross-harness-meaningful knobs. For any other opencode
setting, drop it into `opencodeConfig` in opencode's **own** config shape — it is
deep-merged into the generated document last, so it overrides the modeled keys on conflict:

```json
{
  "harness": "opencode",
  "config": {
    "model": "deepseek/deepseek-v4-pro",
    "effortLevel": "high",
    "opencodeConfig": {
      "theme": "tokyonight",
      "agent": { "build": { "temperature": 0.2 } }
    }
  }
}
```

`opencodeConfig` must be a JSON object (a non-object is an error). It is the one escape
hatch you need for opencode: the modeled keys cover the common cases tersely and stay
consistent with the other harnesses, and anything else is written in opencode's own format
here.
