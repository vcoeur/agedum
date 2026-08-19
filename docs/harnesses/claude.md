---
title: Claude harness · agedum
description: How agedum drives Claude Code — wrapper-mode resolution (AGENTS.md → CLAUDE.md and skills binds, project and global; plus the read-only settings.json + hook-scripts overlay) and the provider config that repoints Claude at a custom endpoint, model, and auth.
---

# Claude

Claude Code discovers its context purely from the filesystem, so agedum injects files at
the paths Claude already reads and appends nothing to your command. This is the reference
harness: pure binds, each [scope](../source-shape.md#scopes) at its own native location.

## Wrapper resolution { #wrapper-resolution }

`agedum --wrapper claude -- claude …` injects:

| Source | Injected at |
|---|---|
| project `AGENTS.md` | `<root>/CLAUDE.md` |
| project `.agents/skills/` | `<root>/.claude/skills/` |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.claude.md` overlay) | `$CLAUDE_CONFIG_DIR/CLAUDE.md` (default `~/.claude/CLAUDE.md`) |
| global `~/.config/agents/skills/` | `$CLAUDE_CONFIG_DIR/skills/` (default `~/.claude/skills/`) |
| global `~/.config/agents/claude/settings.json` (optional overlay) | `$CLAUDE_CONFIG_DIR/settings.json` — **read-only** |
| global `~/.config/agents/claude/scripts/` (optional overlay) | `$CLAUDE_CONFIG_DIR/scripts/` — **read-only** |

- Each scope lands at **its own** location — never concatenated. The project `CLAUDE.md`
  carries only project instructions; the user `CLAUDE.md` only the global ones. Claude
  reads both and applies its own precedence. See [Scopes](../source-shape.md#scopes).
- The global `CLAUDE.md` is the base `~/.config/agents/AGENTS.md` with an optional
  `AGENTS.claude.md` overlay appended (user scope only; the project `CLAUDE.md` takes no
  overlay). See [per-harness overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope).
- For each skill, the base `SKILL.md` is merged with an optional `SKILL.claude.md` overlay
  (front-matter union with overlay winning, bodies concatenated), then task files and
  scripts are copied verbatim.
- The user config dir gets up to four global overlays: `CLAUDE.md`, `skills/`, and — when a
  `claude/` overlay is present in the source root — the read-only `settings.json` + `scripts/`
  ([below](#claude-overlay)). Your `~/.claude.json` auth and anything else in the dir are
  untouched.
- `extra_args`: **none**. The command runs exactly as you wrote it.

```bash
# Interactive Claude with project + global context injected:
agedum --wrapper claude -- claude

# Headless review with a specific model:
agedum --wrapper claude -- claude --model sonnet -p "review this change"
```

## Claude overlay — settings.json + hook scripts { #claude-overlay }

`settings.json` (permissions, hooks, statusline) and the hook `scripts/` it calls are
Claude-specific config, not agent-neutral source, so they live in a `claude/` corner of the
global source root — `~/.config/agents/claude/settings.json` and
`~/.config/agents/claude/scripts/`. At **global scope** agedum binds each **read-only** into
the user config dir (`$CLAUDE_CONFIG_DIR/settings.json` + `/scripts/`), gated on the source
existing: a host with no `claude/` overlay gets no binds and nothing changes.

Two consequences of the read-only bind, both by design:

- **agedum never writes into `~/.claude/`** — it bind-mounts, exactly as for `CLAUDE.md` and
  skills. That is why the overlay is sourced from the writable `~/.config/agents/` root rather
  than being copied straight into `~/.claude/`: under a [sandbox](../provider.md) launch (and
  some managed environments) `~/.claude/` is mounted read-only, and a writer that targets it
  fails with `EROFS`.
- **Claude never rewrites user-scope `settings.json` in place** — change it at the source and
  re-inject. Session-level permission grants still land in the project
  `.claude/settings.local.json` (a separate, untracked layer), so this does not get in the way.

The overlay files are typically shipped by [agentsconf](https://github.com/vcoeur/agentsconf);
agedum only consumes whatever has landed under `~/.config/agents/claude/`.

## Provider config { #provider-config }

A [provider](../provider.md) config repoints Claude Code at a custom endpoint, model, and
auth. Native Claude (the real Anthropic API, your own login) needs **no** provider — just
run `claude`; a provider is only for overriding the endpoint/model/auth.

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

When `baseUrl` is empty the harness sets no provider **environment** — a native launch keeps
Claude Code's own endpoint, auth and model resolution. The two flag-shaped keys, `mcpServers`
and `settings`, are still applied: they are configuration, not provider overrides, and a
native launcher is exactly where they earn their keep. Otherwise the `config` block maps to
environment variables:

| `config` key | Set as |
|---|---|
| `baseUrl` | `ANTHROPIC_BASE_URL` |
| `authStyle` + `secretEnv` | `ANTHROPIC_AUTH_TOKEN` (bearer, default) or `ANTHROPIC_API_KEY` (apikey); the other is unset |
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
| `foldSystemMessages` | `AGEDUM_FOLD_SYSTEM_MESSAGES=1` — see [below](#fold-proxy) |
| `upstreamApi` | `openai-completions` → `AGEDUM_TRANSLATE_OPENAI=1` (see [below](#translate-proxy)); `anthropic-messages` / unset → no translation |
| `openaiPromptCacheKey` | `AGEDUM_OPENAI_PROMPT_CACHE_KEY=1` — inject a per-launch `prompt_cache_key` (translate proxy only; see [below](#translate-proxy)) |
| `openaiThinking` | `AGEDUM_OPENAI_THINKING=<mode>` — `"toggle"` maps Anthropic `thinking` on/off (translate proxy only) |
| `autoCompactWindow` (> 0) | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` |
| `extraEnv` (object) | arbitrary env for the claude child, stringified + applied last (escape hatch, e.g. `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, `DISABLE_COMPACT`) |
| `mcpServers` (object) | MCP servers, in the canonical cross-harness vocabulary — see [below](#mcp) |
| `settings` (object) | an additional Claude Code settings layer — see [below](#settings) |

- `secretEnv` (mapped to the auth token) must be present in the [env file](../provider.md#the-env-file);
  `baseUrl` without a `secretEnv` is an error.
- `upstreamApi: "openai-completions"` and `foldSystemMessages` are mutually exclusive (the
  translator already produces a clean top-level `system`); setting both is an error.
- `CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY,MANTLE}` are always unset defensively. Empty
  strings, zero, and `false` are omitted.
- Sizing the context window of a third-party model (verified against Claude Code 2.1.205):
  - `maxContextTokens` is honoured **only when the model id does not start with `claude-`**;
    a first-party id ignores it and keeps Claude Code's own window. It is the knob that
    lifts a third-party model off the 200,000-token default.
  - `autoCompactWindow` is clamped to `[100_000, 1_000_000]`, then capped to the context
    window — the effective threshold is `min(maxContextTokens, autoCompactWindow)`. Setting
    `maxContextTokens` above 1,000,000 therefore buys headroom between the compaction
    trigger and the upstream's hard limit, not a bigger meter.
  - A model id matching `[1m]` (e.g. DeepSeek's `deepseek-v4-pro[1m]`) is special-cased to
    1,000,000 tokens without `maxContextTokens`, unless `disable1M` is set.
- `--prompt`/`--run` seed an interactive vs `--print` run — see the
  [prompt-seeding table](../provider.md#prompt-seeding).

## MCP servers { #mcp }

`mcpServers` is the [canonical cross-harness vocabulary](../provider.md#mcp); Claude Code's
own stdio dialect *is* that vocabulary, so stdio entries pass through untouched and a remote
entry becomes `{type, url, headers}`. The document is appended to the command as
`--mcp-config '<json>'` (the flag takes a JSON **string**, so nothing is written to disk).

```json
"config": {
  "mcpServers": {
    "nodum": { "command": "nodum", "args": ["mcp", "serve"],
               "env": { "NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}" } }
  }
}
```

- **Additive, not exclusive.** `--strict-mcp-config` is never passed, so the servers in
  `~/.claude/settings.json` and a project `.mcp.json` still load alongside these.
- **`${VAR}` is left verbatim** — Claude Code expands it against its own environment
  (`${VAR}` and `${VAR:-default}`), so no token lands in argv. Name the var in `requiredEnv`
  so agedum copies it out of the env file and the launch fails loudly when it is missing.
- Claude Code spawns a stdio server with the inherited environment plus the entry's own
  `env`, which is applied last and wins. Inheritance is not guaranteed — it is suppressed by
  `CLAUDE_CODE_MCP_ALLOWLIST_ENV` and under `CLAUDE_CODE_ENTRYPOINT=local-agent`, and even
  the inheriting path scrubs `OTEL_*` / `INPUT_*` / `CLAUDE_BG_*` — so declare what the
  server needs rather than relying on it.

## Settings layer { #settings }

`settings` is a Claude Code settings document appended to the command as
`--settings '<json>'`. Like `--mcp-config`, the flag takes a JSON **string**, so nothing is
written to disk.

```json
"config": {
  "settings": { "model": "opus" }
}
```

- **Additive, and it wins.** The document is an extra settings layer merged over the user's
  own `settings.json` key-by-key — it does not replace it. A launcher can therefore pin one
  key while the user's permissions, hooks and statusline still apply.
- **This is how a native launcher pins its default model.** The `model` env mapping in the
  table above needs a `baseUrl`; a native launch has none, so `config.model` would be
  ignored. `settings: {"model": "opus"}` is the supported way to say "this launcher is Opus"
  — see the two-launcher pattern below.
- **`${VAR}` is left verbatim**, as with `--mcp-config` — Claude Code expands it against its
  own environment, so no token lands in argv.
- **`--providers` reads it too.** The roster's model column falls back to `settings.model`
  when `config.model` is absent, so a native launcher lists the model it actually opens on.

Two launchers differing only in default model, sharing everything else through a base:

```json
// providers/claude/opus.json
{ "extends": "base/mcp-nodum.json", "harness": "claude", "favorite": true,
  "config": { "settings": { "model": "opus" } } }

// providers/claude/fable.json
{ "extends": "base/mcp-nodum.json", "harness": "claude", "favorite": true,
  "config": { "settings": { "model": "fable" } } }
```

## System-role fold proxy { #fold-proxy }

Some Anthropic-compatible endpoints (notably DeepSeek's `/anthropic`) accept only
`user` / `assistant` in the `messages` array and reject a `system` role with
`400 unknown variant 'system'`. Claude Code, however, emits hook `additionalContext`
(e.g. a SessionStart reminder) as a `system`-role message *inside* `messages`, alongside
the genuine top-level `system` prompt — the real Anthropic API and lenient endpoints
tolerate it; strict ones do not.

Setting `foldSystemMessages: true` in the config sets `AGEDUM_FOLD_SYSTEM_MESSAGES=1`. At
run time the claude launch interposes a local `127.0.0.1` reverse proxy in front of
`ANTHROPIC_BASE_URL`: it folds every `system`-role message into the top-level `system`
field (always valid — the endpoint already accepts that field), forwards to the real
upstream, and streams the response back close-delimited (SSE-safe). It serves one request
per connection and treats a client hang-up — an interrupted generation, a reaped idle
socket — as routine, so a dropped peer never surfaces as a stderr traceback. Chunked
request bodies are de-chunked before forwarding, and a hung upstream is bounded by a
generous per-socket-op timeout (300 s — far above the API's streaming ping cadence, so it
only ever fires on a genuinely dead peer). The proxy lives only for the duration of the
wrapped command, and is a no-op for other harnesses and when the flag is unset.

## OpenAI-only upstream — translation proxy { #translate-proxy }

Claude Code only speaks the Anthropic Messages protocol, but some gateways expose only an
OpenAI `/v1/chat/completions` surface — or their Anthropic `/v1/messages` surface is broken
for the traffic Claude Code sends. (OpenCode Go is the motivating case: its `/messages`
translation drops the tool-function `name`, so any request carrying tools — which Claude
Code always sends — fails with a 400, while its `/chat/completions` works.)

Setting `upstreamApi: "openai-completions"` sets `AGEDUM_TRANSLATE_OPENAI=1`. At run time the
claude launch interposes a local `127.0.0.1` reverse proxy in front of `ANTHROPIC_BASE_URL`
that **translates between the two protocols**, reusing the same threading/lifecycle skeleton
as the fold proxy:

- **Request** — Anthropic Messages → OpenAI Chat Completions: top-level `system` becomes a
  leading system message; content blocks, `tool_use`/`tool_result`, and images (base64 data
  URLs) are mapped; `tools[].input_schema` → `function.parameters` (rejected JSON-Schema
  `format` keywords stripped); the path `/v1/messages` → `/v1/chat/completions`; and the
  `x-api-key` key is resent as `Authorization: Bearer`. The config's `model` overrides the
  request model and `effortLevel` is injected as `reasoning_effort`. With `openaiPromptCacheKey`,
  a per-launch `prompt_cache_key` is added to every request (a prefix-cache routing hint honoured
  by Moonshot/Kimi); with `openaiThinking: "toggle"`, Anthropic `thinking` is mapped to an on/off
  `thinking: {"type": …}` param instead of being dropped.
- **Response** — OpenAI → Anthropic, streaming and non-streaming. A streamed OpenAI SSE
  response is re-serialised to the Anthropic event sequence (`message_start` →
  `content_block_*` → `message_delta` → `message_stop`), with tool-call arguments streamed as
  `input_json_delta` fragments. Upstream errors are translated to the Anthropic error
  envelope with the status preserved, so the real cause still surfaces in Claude Code.

Example config (OpenCode Go, `kimi-k2.7-code`):

```json
{
  "harness": "claude",
  "secretEnv": "OPENCODE_GO_API_KEY",
  "requiredEnv": ["OPENCODE_GO_API_KEY"],
  "config": {
    "baseUrl": "https://opencode.ai/zen/go",
    "authStyle": "apikey",
    "upstreamApi": "openai-completions",
    "model": "kimi-k2.7-code",
    "effortLevel": "high"
  }
}
```

Scope and limits (v1): the translation covers the subset Claude Code actually emits, not the
full Anthropic API. Per-block `cache_control` has no OpenAI equivalent and is dropped, but
session-level prefix caching is hinted via `openaiPromptCacheKey` (above) and OpenAI
`cached_tokens` are surfaced as Anthropic `cache_read_input_tokens`. Extended `thinking` is
dropped by default but can be mapped on/off with `openaiThinking: "toggle"` (for endpoints that
accept a `thinking: {"type": …}` param — do not enable it against an always-think model, which
errors on `"disabled"`). `/v1/messages/count_tokens` is answered locally with a coarse
chars/4 estimate; input-token usage in `message_start` is best-effort. `--dry-run` shows the
interposed translator and its real upstream. The proxy lives only for the duration of the
wrapped command, and is a no-op for other harnesses and when `upstreamApi` is unset.
