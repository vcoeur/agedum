---
title: Provider mode · agedum
description: Launch a harness from a provider config JSON — agedum resolves the provider's env from a .env, validates the required vars, sets the provider/model/auth environment, and launches the harness inside the virtual-file context.
---

# Provider mode

```text
agedum <provider-name|config.json> [--env <file>] [--dry-run] [harness args...]
```

Provider mode is the primary way to launch an agent. agedum reads a **provider config
JSON**, resolves the provider's secrets from a `.env`, sets the provider/model/auth
environment, and launches the harness named in the config — all in one process, inside
the same [virtual-file context](harnesses.md) wrapper mode uses. There is no generated
launcher script: the config JSON is read at run time.

```bash
agedum claude-deepseek-auto                 # resolve the named provider, launch claude
agedum claude-deepseek-auto -p "review this"  # extra args go to the harness
agedum ./providers/my-claude.json            # a path instead of a name
agedum claude-deepseek-auto --dry-run        # print the resolved env + argv, don't launch
```

## Resolving the provider

The single positional argument is **a name or a path**:

- **path** — it contains `/` or ends in `.json`. Absolute as-is, otherwise relative to
  the current directory.
- **name** — anything else. Resolved to
  `${AGENTS_PROVIDERS_DIR:-~/.config/agents/providers}/<name>.json`.

Any token after the provider that isn't an agedum flag is passed to the harness verbatim
(`agedum claude-deepseek-auto -p "hi"` runs `claude -p "hi"`). `--env` and `--dry-run` are
agedum's own flags and may appear **before or after** the provider; to forward a literal
`--dry-run`/`--env` to the harness, put it after a `--` (`agedum claude-deepseek-auto -- --dry-run`).

## The env file

Secrets are read from `${AGENTS_ENV_FILE:-~/.config/agents/.env}`, overridable per-run
with `--env <file>`. It is a simple `KEY=VALUE` file (an optional `export ` prefix and
surrounding quotes are honoured; `#` lines and blanks are skipped). Every variable named
in the config's `requiredEnv` (plus `secretEnv`) must be present and non-empty, or agedum
fails fast with a clear message before launching.

Unlike the retired `--build-script` codegen — which emitted a wrapper that sourced the
`.env` itself, so agedum never saw a token — provider mode reads the env file into the
agedum process and sets the resolved values in the child environment.

## Config shape

The config is the condash-style agent envelope:

```json
{
  "harness": "claude",
  "name": "Claude Deepseek Auto",
  "slug": "claude-deepseek-auto",
  "secretEnv": "DEEPSEEK_API_KEY",
  "requiredEnv": ["DEEPSEEK_API_KEY"],
  "config": { "...": "per-harness options" }
}
```

| Field | Meaning |
|---|---|
| `harness` | `claude`, `kimi`, or `opencode`. Selects the translation **and** the harness to launch; read from the file (there is no `--harness` flag). |
| `secretEnv` | The env var holding the API token. For `claude` it is mapped to `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`; for `kimi` / `opencode` it is passed through under its own name. |
| `requiredEnv` | Vars validated and exported into the child. `secretEnv` is always appended if not listed. Declare a provider's API-key var here so `kimi` / `opencode` (which read it from the environment) see it. |
| `config` | The per-harness option block — see below. |
| `name` / `slug` | Labels; `slug` (else `name`, else the harness) names the provider in error and `--dry-run` messages. |

Save the file as `<slug>.json` under `~/.config/agents/providers/` (or anywhere, and
launch it by path), put the API token in `~/.config/agents/.env`, then
`agedum <slug> --dry-run` to check it before launching for real.

## Recipes

A complete, working config for each harness. The `config` block's keys are detailed in
[Per-harness `config` mapping](#per-harness-config-mapping) below.

### claude against a custom endpoint

```json
{
  "harness": "claude",
  "slug": "claude-deepseek",
  "secretEnv": "DEEPSEEK_API_KEY",
  "requiredEnv": ["DEEPSEEK_API_KEY"],
  "config": {
    "baseUrl": "https://api.deepseek.com/anthropic",
    "model": "deepseek-v4-pro",
    "foldSystemMessages": true
  }
}
```

`baseUrl` + `secretEnv` repoint Claude Code at the endpoint; `foldSystemMessages` is
needed for strict Anthropic-compat upstreams (see [below](#foldsystemmessages-strict-anthropic-compat-upstreams)).
Native Claude (the real Anthropic API, your own login) needs no provider at all — just run
`claude`; a provider is only for overriding the endpoint/model/auth.

### kimi against Moonshot

```json
{
  "harness": "kimi",
  "slug": "kimi",
  "secretEnv": "MOONSHOT_API_KEY",
  "requiredEnv": ["MOONSHOT_API_KEY"],
  "config": { "model": "kimi-k2", "thinking": true }
}
```

kimi reads its token from the environment, so the key must be in `requiredEnv`; the
`config` knobs become appended CLI flags (`--model kimi-k2 --thinking`).

### opencode with per-agent routing

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

opencode resolves provider credentials from its **own** auth store
(`opencode auth login`), so a key is only in `requiredEnv` when opencode itself reads it
from the environment. The `config` block is translated into opencode's
`OPENCODE_CONFIG_CONTENT` document; for anything not modeled, use
[`opencodeConfig`](#opencodeconfig-anything-agedum-doesnt-model).

## `--dry-run`

Prints the full resolved launch without running it, so you can see exactly what context
the harness is given. It is grouped by **scope** (project / global); under each, every
source (`AGENTS.md`, `.agents/skills/`) is listed with its **disposition**: `→ <dest>`
when injected, `read in place` when the harness reads it natively (kimi/opencode read the
project `AGENTS.md` in place — never invisible), `→ kimi agent file` for the kimi
`--agent-file`, or an explicit note when a scope contributes nothing. Project-scope paths
display relative to the cwd; global-scope stays `~`-absolute. The resolved config (env
vars; opencode's `OPENCODE_CONFIG_CONTENT` pretty-printed) and the final command are shown
too. For a kimi provider run from a project root:

```text
provider   Kimi
harness    kimi
env file   ~/.config/agents/.env

project scope · ~/src/foo
  AGENTS.md         read in place (read natively — not injected)
  .agents/skills/   → .kimi/skills/

global scope
  ~/.config/agents/AGENTS.md   → kimi agent file (passed via --agent-file)
  ~/.agents/skills/            → ~/.kimi/skills/

command
  kimi
  + agedum appends: --agent-file /tmp/agedum-kimi-dry-…/agent.yaml
```

For an **opencode** provider the resolved config is shown as indented JSON (the `environment`
section), and a scope with no sources is stated explicitly, e.g.:

```text
environment
  OPENCODE_CONFIG_CONTENT
    {
      "model": "deepseek/deepseek-v4-pro",
      "provider": { "deepseek": { "models": { "deepseek-v4-pro": { "options": { "reasoningEffort": "max" } } } } }
    }

project scope · ~
  (no AGENTS.md or .agents/skills found here)

global scope
  ~/.config/agents/AGENTS.md   → ~/.config/opencode/AGENTS.md
  ~/.agents/skills/            → ~/.config/opencode/skills/
```

This is the same view [wrapper mode](harnesses.md) shows — how agedum renders the
agent-neutral [source](source-shape.md) for the harness. Nothing is written to your real
tree: the listed destinations exist only inside the launched process's
[mount namespace](internals.md).

## Per-harness `config` mapping

### claude

When `baseUrl` is empty the harness runs bare (no provider overrides). Otherwise:

| `config` key | Set as |
|---|---|
| `baseUrl` | `ANTHROPIC_BASE_URL` |
| `authStyle` + `secretEnv` | `ANTHROPIC_AUTH_TOKEN` (bearer) or `ANTHROPIC_API_KEY` (apikey); the other is unset |
| `model` | `ANTHROPIC_MODEL` |
| `smallFastModel` | `ANTHROPIC_SMALL_FAST_MODEL` |
| `haikuAlias` / `sonnetAlias` / `opusAlias` | `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL` |
| `subagentModel` | `CLAUDE_CODE_SUBAGENT_MODEL` |
| `maxContextTokens` (> 0) | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |
| `effortLevel` | `CLAUDE_CODE_EFFORT_LEVEL` |
| `disableCaching` | `DISABLE_PROMPT_CACHING=1` |
| `disable1M` | `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` |
| `disableAdaptiveThinking` | `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1` |
| `disableTelemetry` | `DISABLE_TELEMETRY=1` |
| `disableErrorReporting` | `DISABLE_ERROR_REPORTING=1` |
| `disableClaudeApiSkill` | `CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL=1` |
| `foldSystemMessages` | `AGEDUM_FOLD_SYSTEM_MESSAGES=1` — see below |

`CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY,MANTLE}` are always unset defensively. Empty
strings, zero, and `false` are omitted.

#### `foldSystemMessages` — strict Anthropic-compat upstreams

Some Anthropic-compatible endpoints (notably DeepSeek's `/anthropic`) accept only
`user` / `assistant` in the `messages` array and reject a `system` role with
`400 unknown variant 'system'`. Claude Code, however, emits hook `additionalContext`
(e.g. a SessionStart reminder) as a `system`-role message *inside* `messages`, alongside
the genuine top-level `system` prompt — the real Anthropic API and lenient endpoints
tolerate it; strict ones do not.

Setting `foldSystemMessages: true` sets `AGEDUM_FOLD_SYSTEM_MESSAGES=1`. At run time the
claude launch interposes a local `127.0.0.1` proxy: it folds every `system`-role message
into the top-level `system` field (always valid — the endpoint already accepts that
field), forwards to the real `baseUrl`, streams the response back unchanged (SSE-safe),
and is torn down when the harness exits. `ANTHROPIC_BASE_URL` still points at the real
upstream; the proxy is interposed transparently.

### kimi

kimi's knobs become **appended CLI flags** on the launched command:

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `thinking` | `--thinking` (true) / `--no-thinking` (false) |
| `plan` | `--plan` (true) |
| `configInline` | `--config <value>` |

The token (`secretEnv`) reaches kimi via the `requiredEnv` export.

### opencode

| `config` key | Effect |
|---|---|
| `model` | `model` field of `OPENCODE_CONFIG_CONTENT` |
| `disableExternalSkills` | `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` |
| `defaultOptions.{reasoningEffort,textVerbosity,reasoningSummary}` | the default model's `provider.<id>.models.<model>.options` |
| `effortLevel` (flat alias) | the default model's `reasoningEffort` (explicit `defaultOptions.reasoningEffort` wins) |
| `agentOptions[]` | per-agent `agent.<name>` model + options; `primary: true` sets `mode: "primary"` for custom (non-built-in) agents |
| `providerDef` | an explicit provider block `provider.<id> = { npm, options: { baseURL, apiKey } }` with the key resolved from the environment — see below |
| `opencodeConfig` | a literal opencode config object, deep-merged into the document last (wins on conflict) — see below |
| `emitTranscript` | inject the bundled transcript-capture plugin (default **on**); set `false` to opt out — see below |

The document is set as a single `OPENCODE_CONFIG_CONTENT` env var (no file written).

#### `emitTranscript` — in-band transcript capture (default on)

opencode runs as a full-screen alternate-screen TUI, so a terminal capturer (condash,
`script`, tmux, asciinema) only ever sees the current frame — the conversation that scrolls
inside the TUI is repainted, never retained. agedum **ships and auto-injects** a small opencode
plugin (`agedum/assets/opencode/transcript-osc.js`) that streams each finalized message into the
terminal as a **neutral OSC escape** the terminal ignores for display:

```
ESC ] 7373 ; agent-transcript ; <frameId> ; <i> ; <n> ; <base64piece> BEL
```

A capturer recovers a clean transcript by reassembling the base64 pieces and decoding the JSON
frames (`{v,t:"msg",sid,mid,role,text}` / `{v,t:"end"}`). The protocol **names no viewer**, so
agedum stays viewer-agnostic — any capturer can consume it. The plugin path is appended to
`OPENCODE_CONFIG_CONTENT.plugin` (unioned with any `opencodeConfig.plugin`); agedum's bwrap
launch binds the whole filesystem, so the bundled path resolves inside the namespace. Set
`"emitTranscript": false` to disable.

#### `providerDef` — declare the provider + key inline

By default an opencode `model` like `openrouter/deepseek/deepseek-v4-pro` relies on
opencode resolving the `openrouter` provider from its **own** auth store
(`opencode auth login`). `providerDef` instead **defines the provider in the config**
and resolves the API key from the environment, so no prior login is needed:

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

The provider block is deep-merged with any per-model reasoning options. The key's
**value** (not a `{env:…}` placeholder) is written into `provider.<id>.options.apiKey`,
because opencode's `{env:…}` substitution is unreliable for a custom provider's
`options.apiKey`. This is the same in-process token handling `claude` already uses for
`ANTHROPIC_AUTH_TOKEN`; `apiKeyEnv` is auto-added to the validated `requiredEnv`, and
the resulting `OPENCODE_CONFIG_CONTENT` is masked in `--dry-run`. (Keys containing `"`
or `\` would break the surrounding JSON; standard `sk-or-…` keys are fine.)

#### `opencodeConfig` — anything agedum doesn't model

The keys above are the common, cross-harness-meaningful knobs. For any other opencode
setting, drop it into `opencodeConfig` in opencode's **own** config shape — it is
deep-merged into the generated document last, so it overrides the modeled keys on
conflict:

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
consistent with the other harnesses, and anything else is written in opencode's own
format here.
