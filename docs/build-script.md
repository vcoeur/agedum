---
title: Build-script mode · agedum
description: Compile a provider config JSON into a standalone shell wrapper that sources a .env, validates the required env vars, exports the provider/model/auth environment, and execs agedum --wrapper.
---

# Build-script mode

```text
agedum --build-script [--check] <config.json> [output.sh]
```

`--build-script` turns a **provider config JSON** into a self-contained bash wrapper.
The wrapper, when run, sources a `.env`, validates the required variables, exports the
provider/model/auth environment for the harness, and finally `exec`s
`agedum --wrapper <harness> -- <harness> "$@"` — composing this codegen step with the
[wrapper mode](harnesses.md) that injects your skills and instructions.

This is how a content repo (e.g. an `agentsconf`) builds its provider launchers:
`providers/<name>.json` → `bin/<name>.sh`. agedum stays a pure translator — it never
reads a token at run time. All secret handling lives in the generated shell.

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
| `harness` | `claude`, `kimi`, or `opencode`. Selects the translation; this is the build input, so it is read from the file (unlike wrapper mode, where the flag is authoritative). |
| `secretEnv` | The env var holding the API token. For `claude` it is mapped to `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`; for `kimi` / `opencode` it is passed through under its own name. |
| `requiredEnv` | Vars the generated wrapper validates and exports. `secretEnv` is always appended if not already listed. Declare a provider's API-key var here so `kimi` / `opencode` (which read it from the environment) see it. |
| `config` | The per-harness option block — see below. |
| `name` / `slug` | Labels; `slug` (else `name`, else the harness) names the provider in the generated comment + error messages. |

## What the generated wrapper does

```bash
#!/usr/bin/env bash
set -euo pipefail

env_file="${AGENTS_ENV_FILE:-$HOME/.config/agents/.env}"   # override with AGENTS_ENV_FILE
if [[ ! -f "$env_file" ]]; then
  echo 'claude-deepseek-auto: env file not found:' "$env_file" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$env_file"
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY is required by provider claude-deepseek-auto but is not set}"

export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY"
# … the rest of the per-harness mapping …

exec agedum --wrapper claude -- claude "$@"
```

- **`.env` location** — `${AGENTS_ENV_FILE:-$HOME/.config/agents/.env}`. Set
  `AGENTS_ENV_FILE` to point elsewhere; no rebuild needed.
- **Validation** — each `requiredEnv` var is checked with `${VAR:?…}` (fails fast with
  a clear message if unset or empty) and exported.
- **Passthrough** — `"$@"` forwards any args you give the wrapper to the harness.
- **No env block** is emitted when a provider declares no secrets (e.g. native Claude),
  so the wrapper just `exec`s the harness.

## Per-harness `config` mapping

### claude

When `baseUrl` is empty the wrapper runs bare (`exec agedum --wrapper claude -- claude`).
Otherwise:

| `config` key | Exported as |
|---|---|
| `baseUrl` | `ANTHROPIC_BASE_URL` |
| `authStyle` + `secretEnv` | `ANTHROPIC_AUTH_TOKEN` (bearer) or `ANTHROPIC_API_KEY` (apikey); the other is `unset` |
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

`CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY,MANTLE}` are always `unset` defensively. Empty
strings, zero, and `false` are omitted.

#### `foldSystemMessages` — strict Anthropic-compat upstreams

Some Anthropic-compatible endpoints (notably DeepSeek's `/anthropic`) accept only
`user` / `assistant` in the `messages` array and reject a `system` role with
`400 unknown variant 'system'`. Claude Code, however, emits hook `additionalContext`
(e.g. a SessionStart reminder) as a `system`-role message *inside* `messages`, alongside
the genuine top-level `system` prompt — the real Anthropic API and lenient endpoints
tolerate it; strict ones do not.

Setting `foldSystemMessages: true` exports `AGEDUM_FOLD_SYSTEM_MESSAGES=1`. At run time
the [wrapper](harnesses.md) reads that flag and, for the claude harness, interposes a
local `127.0.0.1` proxy: it folds every `system`-role message into the top-level
`system` field (always valid — the endpoint already accepts that field), forwards to the
real `baseUrl`, streams the response back unchanged (SSE-safe), and is torn down when the
harness exits. `ANTHROPIC_BASE_URL` in the generated wrapper still points at the real
upstream; the proxy is interposed transparently by agedum.

### kimi

kimi's knobs become **appended CLI flags** on the exec line:

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `thinking` | `--thinking` (true) / `--no-thinking` (false) |
| `plan` | `--plan` (true) |
| `configInline` | `--config <value>` |

### opencode

| `config` key | Effect |
|---|---|
| `model` | `model` field of `OPENCODE_CONFIG_CONTENT` |
| `disableExternalSkills` | `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` |
| `defaultOptions.{reasoningEffort,textVerbosity,reasoningSummary}` | the default model's `provider.<id>.models.<model>.options` |
| `effortLevel` (flat alias) | the default model's `reasoningEffort` (explicit `defaultOptions.reasoningEffort` wins) |
| `agentOptions[]` | per-agent `agent.<name>` model + options; `primary: true` sets `mode: "primary"` for custom (non-built-in) agents |
| `extraConfigJson` | parsed and deep-merged into the document |

The document is emitted as a single `OPENCODE_CONFIG_CONTENT` env var (no file written).

## Drift guard — `--check`

`--check` regenerates the script in memory and compares it to the committed `output.sh`,
exiting non-zero if they differ. Run it in CI so a stale `bin/<name>.sh` (someone edited
the JSON but forgot to regenerate) fails the build:

```bash
agedum --build-script --check providers/claude-deepseek-auto.json bin/claude-deepseek-auto.sh
```

Generation is deterministic, so `--check` is byte-exact.
