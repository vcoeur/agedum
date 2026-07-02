---
title: codex harness · agedum
description: How agedum drives codex (OpenAI Codex CLI) — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md to ~/.codex/AGENTS.md, skills to .codex/skills) and the provider config mapped to codex's -c/-m flags, with a custom model_providers endpoint and a generated flash subagent file.
---

# codex

[codex](https://github.com/openai/codex) (the OpenAI Codex CLI, `@openai/codex`) is a
terminal coding agent. Like [opencode](opencode.md), [Cline](cline.md),
[reasonix](reasonix.md), and [pi](pi.md), it is **pure path-discovery** in wrapper mode — it
reads `AGENTS.md` and skills from fixed locations under its home dir (`$CODEX_HOME`, default
`~/.codex`) and needs no flags. So the project instructions stay in place and everything else
is a bind.

## Wrapper resolution { #wrapper-resolution }

| Source | Injected at |
|---|---|
| project `AGENTS.md` | *(not injected — read natively, work-dir→root walk)* |
| project `.agents/skills/` | `<root>/.codex/skills/` |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.codex.md` overlay) | `~/.codex/AGENTS.md` |
| global `~/.config/agents/skills/` | `~/.codex/skills/` |

- **Project instructions** — codex walks from the work dir up to the project root collecting
  `AGENTS.md` and folds a limited slice into the first turn, so the project-root `AGENTS.md` —
  the agent-neutral source, already in place — is read **natively**. agedum injects nothing
  for it (and never could, since the root `AGENTS.md` is git-tracked).
- **Global instructions** — codex reads a user-scope rules file from its home dir at boot
  (`$CODEX_HOME/AGENTS.md`). So the global `AGENTS.md` is bound to `~/.codex/AGENTS.md` — base
  merged with an optional `AGENTS.codex.md`
  [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope). (codex prefers
  an `AGENTS.override.md` if one exists; agedum writes the base name, so define overrides via
  the overlay, not a separate file.) The home dir honours `$CODEX_HOME`.
- **Skills** — codex auto-discovers `SKILL.md` folders from `~/.codex/skills/` (global) and
  `./.codex/skills/` (project). Each skill carries `name`/`description` frontmatter — the
  neutral source shape — compiled with the `SKILL.codex.md` overlay and bound to those two
  dirs.
- `extra_args`: **none** — codex discovers everything from disk; the model and custom endpoint
  ride `-m`/`-c` flags built in provider mode.

```bash
agedum --wrapper codex -- codex            # drive codex with the same source
agedum --wrapper codex --dry-run -- codex  # show what would be injected
```

!!! note "The sandbox makes `~/.codex` writable automatically"
    codex persists session, log, and history state under `~/.codex/`. Under a
    [write-confinement](../wrapper.md#sandbox) launch that dir is made writable as the
    *nearest existing ancestor* of the injected `~/.codex/AGENTS.md` (the same mechanism that
    keeps `~/.claude` writable for Claude) — so no `sandbox.readWrite` entry is needed, as long
    as a global `AGENTS.md` (or the generated flash agent file) is injected.

## Provider config { #provider-config }

codex selects its model with `-m` and reads its API key from a conventional env var, so a
provider config maps to plain flags. codex has **no `--base-url` flag**, but its `-c
key=value` override (each value parsed as TOML, winning over `~/.codex/config.toml`) can
define a custom provider inline — so a custom OpenAI-/Anthropic-compatible endpoint needs **no
generated config file**.

### Built-in / OpenAI provider (no `baseUrl`)

```json
{
  "harness": "codex",
  "slug": "codex-gpt",
  "config": {
    "model": "gpt-5.5"
  }
}
```

| `config` key | Effect |
|---|---|
| `model` | `-m <model>` |
| `secretEnv` value | exported into the child (`requiredEnv`); codex's default `openai` provider reads `OPENAI_API_KEY` |

This launches `codex -m gpt-5.5` against codex's built-in `openai` provider (or whatever
`~/.codex/config.toml` selects). **No key flag is appended and no secret lands in argv** —
agedum exports `secretEnv` under its own name via the normal
[required-env](../provider.md#the-env-file) path. The token is masked in `--dry-run`.

### Custom endpoint (`baseUrl`) { #custom-endpoint }

A custom endpoint becomes a `[model_providers.agedum]` block passed via `-c` overrides and
selected with `-c model_provider=agedum`. `model_provider` cannot be one of codex's reserved
built-in ids (`openai`, `ollama`, `lmstudio`, `amazon-bedrock`), so agedum names it `agedum`.

| `config` key | Effect |
|---|---|
| `baseUrl` | the endpoint → `model_providers.agedum.base_url`; its presence switches on the custom provider |
| `model` | the upstream model id served there → `-m <model>` |
| `chatCompletions` | `true` ⇒ the endpoint speaks **Chat Completions**, so agedum interposes a translation proxy — see below |
| `wireApi` | codex's wire protocol for a **Responses** endpoint — emitted only when set (else codex's default applies). `chat` is **rejected by recent codex** (removed Feb 2026); use `chatCompletions` for a chat endpoint |
| `secretEnv` | referenced as `model_providers.agedum.env_key` (its **name**, never its value) |
| `codexConfig` | a table of arbitrary codex config keys → `-c key=<toml>` overrides (bool bare, int/float bare, everything else quoted). Carries the model metadata codex can't learn from a translated endpoint — chiefly `model_context_window` (the context-meter denominator, since the `/models` probe is answered empty) and `model_supports_reasoning_summaries` / `model_reasoning_summary` (so codex renders the reasoning the proxy surfaces — see [chat proxy](#chat-proxy)) |
| `codexModelCatalog` | a table (`{contextWindow, displayName?, description?}`) that makes codex fully **recognise** the custom `model` — see [model catalog](#model-catalog) |

**The key never lands on disk or in argv** — codex reads it from the env var named by
`env_key`, which agedum exports from the [env file](../provider.md#the-env-file) at launch and
masks in `--dry-run`.

#### Chat-Completions endpoints (DeepSeek etc.) — the translation proxy { #chat-proxy }

Recent codex (≥ Feb 2026) speaks **only** the OpenAI Responses API — `wire_api = "chat"` is
rejected outright. DeepSeek and most OpenAI-compatible providers serve **only** Chat
Completions, so codex cannot reach them directly. Setting **`chatCompletions: true`** makes
agedum interpose a localhost [`ResponsesToChatProxy`](../internals.md) for the session (the
[`FoldProxy`](claude.md) sibling): codex's `base_url` is pointed at the proxy, which translates
codex's `POST /responses` into a `/chat/completions` call upstream and the streamed Chat
deltas back into the Responses SSE events codex consumes (text, function calls, and — for a
thinking model that streams `delta.reasoning_content`, e.g. Kimi K2.7 — a `reasoning` output
item streamed as reasoning-summary events, so codex renders the model's thinking).

```json
{
  "harness": "codex",
  "slug": "codex-deepseek",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "baseUrl": "https://api.deepseek.com/v1",
    "chatCompletions": true,
    "model": "deepseek-v4-pro"
  }
}
```

→ launches `codex -c model_provider="agedum" -c model_providers.agedum.base_url="<proxy>" -c
model_providers.agedum.env_key="DEEPSEEK_API_KEY" -m deepseek-v4-pro`, where `<proxy>` is the
proxy's ephemeral `127.0.0.1` address (codex, inside the bwrap namespace, shares the host
loopback). No `wire_api` is sent — codex speaks Responses to the proxy. The proxy forwards
codex's `Authorization: Bearer <key>` header to the upstream unchanged.

!!! note "Proxy scope + watchpoints"
    The proxy covers the codex loop verified live against DeepSeek — streamed text and
    function-call (tool) turns, single and multi-turn, on both `deepseek-v4-pro` and
    `deepseek-v4-flash` — and against Kimi K2.7 for **reasoning display** (the model's
    `reasoning_content` rendered by codex as a thinking summary). Known limits: (1) the stateful
    **reasoning round-trip** across tool turns is **not** replayed on *input* (inbound
    `reasoning` items are dropped) — the response-side reasoning *display* above is separate and
    works; revisit if a provider errors on missing `reasoning_content`; (2) codex's `GET /models`
    metadata probe is answered with an empty list, so codex logs a benign "metadata not found"
    warning and falls back to default metadata — set the real window (and reasoning-summary
    support) explicitly with `codexConfig` (`model_context_window`,
    `model_supports_reasoning_summaries`), which overrides the fallback; (3) codex rejects a skill
    whose `description` exceeds **1024 chars** and skips it — trim long agedum skill descriptions
    to surface them under codex.

### Model catalog (`codexModelCatalog`) { #model-catalog }

A custom model isn't in codex's bundled catalog, so codex logs *"model metadata … not found"*,
falls back to default metadata, and — crucially — can't populate its **context-usage meter** (the
`X/262k` readout), because the meter reads the window from the *catalog* entry, not from the
`model_context_window` override. `codexConfig` alone therefore can't light up the meter.

**`codexModelCatalog`** closes that gap. When set, agedum runs the installed **`codex debug
models`** to read codex's own catalog, clones its first entry (so the required, version-specific
`base_instructions` and capability flags are correct), renames it to this config's `model`, applies
the table's overrides, writes a one-model catalog to `~/.codex/agedum-model-catalog.json`, and
passes `-c model_catalog_json=<that file>`. codex then recognises the model — warning gone, context
meter live.

| `codexModelCatalog` key | Effect |
|---|---|
| `contextWindow` | `context_window` + `max_context_window` on the cloned entry — the meter denominator |
| `displayName` | the entry's `display_name` (defaults to `model`) |
| `description` | the entry's `description` (optional) |

```json
{
  "harness": "codex",
  "slug": "codex-kimi-code",
  "secretEnv": "KIMI_API_KEY",
  "config": {
    "baseUrl": "https://api.kimi.com/coding/v1",
    "chatCompletions": true,
    "model": "kimi-for-coding",
    "codexModelCatalog": { "contextWindow": 262144, "displayName": "Kimi K2.7 (Code)" }
  }
}
```

!!! note "Graceful + robust"
    Cloning from the **live** `codex debug models` keeps `base_instructions` correct across codex
    versions — nothing codex-internal is hardcoded. If codex can't be queried (absent, error,
    unparseable output), agedum **skips** the catalog and codex uses its fallback metadata — the
    launch never fails on it. The generated catalog carries only the one custom model, so pair it
    with a launcher that pins `-m <model>` (the other bundled models aren't selectable under it).

### Custom subagents (`subagentModel`, `codexAgents`, `codexProjectAgents`) { #subagent-routing }

codex has **no global "route all subagents to a fast model" knob** (openai/codex#19482) and no
inline agent config. What it does have is **custom subagents** — standalone TOML files under
`~/.codex/agents/` (personal) or `.codex/agents/` (project) the primary delegates to on request.
agedum binds agent definitions into those dirs three ways:

- **`subagentModel: <id>`** — sugar for one fast `flash` worker; agedum generates
  `~/.codex/agents/flash.toml` running that model (the template below).
- **`codexAgents: <dir>`** — bind every `*.toml` in `<dir>` (a path under the providers root)
  into `~/.codex/agents/<name>.toml` (**personal** scope — available in every project).
- **`codexProjectAgents: <dir>`** — same, into `.codex/agents/<name>.toml` (**project** scope —
  only in the launch's working tree; overrides a personal agent of the same name).

Source files are bound verbatim, except agedum injects a default **`sandbox_mode = "workspace-write"`**
when the source omits it (matching agedum's write-confined launch; an explicit `sandbox_mode` is
passed through). Two agents resolving to the same target — e.g. a `flash.toml` source colliding
with `subagentModel`'s flash — is a hard error.

```json
{
  "harness": "codex",
  "slug": "codex-deepseek-flash",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "baseUrl": "https://api.deepseek.com/v1",
    "model": "deepseek-v4-pro",
    "codexAgents": "codex/agents"
  }
}
```

→ the same `deepseek-v4-pro` launch as above, plus every `*.toml` in
`<providers-root>/codex/agents/` bound under `~/.codex/agents/`. The `flash` worker as a source
file:

```toml
name = "flash"
description = "Fast, low-cost worker for routine, well-scoped subtasks. Delegate mechanical work here to keep the primary model free for harder reasoning."
developer_instructions = """
You are a fast, cost-efficient worker. Carry out the delegated subtask directly and return a concise, complete result; prefer doing the work over deliberating.
"""
model = "deepseek-v4-flash"
# source omits sandbox_mode → agedum injects sandbox_mode = "workspace-write"
```

!!! note "inert unless the primary delegates"
    codex spawns subagents only when **explicitly asked** ("spawn an agent to do X"), and it
    has no global default that routes work to one automatically. So a bound agent gives the
    primary a delegate to *choose*, but its model is **not** used unless you (or the primary's
    plan) invoke it — unlike opencode's `agentOptions` or pi's `subagents.agentOverrides`, which
    route built-in roles. This is the most faithful mapping codex's subagent model allows today;
    revisit if codex#19482 lands a default-model knob. Every agent file is bound through the
    normal launcher path, so a **project-scope** target is refused over a git-tracked path (it
    must be untracked + gitignored).

**`--prompt`/`--run`.** codex takes a positional prompt to seed an interactive session; the
`exec` subcommand runs it once non-interactively and exits. So `--prompt` seeds the TUI
(`codex "<text>"`) and `--run` runs once and exits (`codex exec "<text>"`). See the
[prompt-seeding table](../provider.md#prompt-seeding).
