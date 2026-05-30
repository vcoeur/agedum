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

Everything after the provider token is passed to the harness verbatim
(`agedum claude-deepseek-auto -p "hi"` runs `claude -p "hi"`). `--env` and `--dry-run`
come **before** the provider.

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

## `--dry-run`

Prints the provider label, harness, env file, the resolved environment (secret values
masked as `***`), the vars to unset, and the final command — without launching. Use it
to verify a provider resolves as expected:

```text
provider: claude-deepseek-auto
harness:  claude
env file: /home/you/.config/agents/.env
  export DEEPSEEK_API_KEY=***
  export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
  export ANTHROPIC_AUTH_TOKEN=***
  …
  unset ANTHROPIC_API_KEY
command:  claude
```

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
| `opencodeConfig` | a literal opencode config object, deep-merged into the document last (wins on conflict) — see below |
| `extraConfigJson` | **deprecated**: the JSON-string form of `opencodeConfig`; parsed and deep-merged |

The document is set as a single `OPENCODE_CONFIG_CONTENT` env var (no file written).

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

Prefer `opencodeConfig` (a real JSON object) over the deprecated `extraConfigJson` (a
JSON string inside the JSON). If both are present, `extraConfigJson` is applied first and
`opencodeConfig` wins. A non-object `opencodeConfig` is an error.
