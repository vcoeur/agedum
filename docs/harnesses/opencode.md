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
| `opencodeConfig.agent.<name>.agentAppend` | per-agent instructions folded onto the end of that agent's `prompt` — see [below](#agentappend) |
| `emitTranscript` | inject the bundled transcript-capture plugin (default **on**); set `false` to opt out — see [below](#emittranscript) |
| `mcpServers` | MCP servers in the canonical cross-harness vocabulary, translated into opencode's `mcp` block — see [below](#mcp) |

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

### `failover` — mechanical provider-wall failover { #failover }

A top-level `failover` block (sibling of `requiredEnv`) makes agedum start a local
**failover proxy** for the launch and point the routed providers' `options.baseURL` at it
(`<proxy>/oc/<id>`; the built-in `openai` provider is overlaid the same way, OAuth
untouched). The proxy forwards the primary attempt verbatim and — when it hits an
**admission wall** before any byte reached opencode (a `detect.status` code, or a 4xx whose
first 2 KB carries a `detect.messages` substring) — re-issues the request down the model's
`chains` with per-rung auth, model id, and effort options, so the session lands on a
surviving rung and opencode never sees the 429 to retry. Chain exhaustion returns the last
upstream error verbatim (native retry/death behaviour, never worse); image-bearing requests
walk only `vision: true` rungs; a per-launch rung pin skips a walled primary on later
requests. `maxWalk` caps rungs tried per request.

```json
{
  "harness": "opencode",
  "requiredEnv": ["KIMI_API_KEY"],
  "config": {
    "providerDef": [
      { "id": "kimi-coding", "npm": "@ai-sdk/openai-compatible", "baseUrl": "https://api.kimi.com/coding/v1", "apiKeyEnv": "KIMI_API_KEY" }
    ],
    "opencodeConfig": {
      "agent": { "main": { "mode": "primary", "model": "kimi-coding/k3" } }
    }
  },
  "failover": {
    "detect": { "status": [429, 402], "messages": ["usage limit", "quota", "insufficient balance", "image"] },
    "maxWalk": 3,
    "vision": { "kimi-coding/k3": true, "kimi-coding/k3-low": true },
    "chains": { "kimi-coding/k3": ["kimi-coding/k3-low"] },
    "rungOptions": {
      "kimi-coding/k3-low": { "thinking": { "type": "enabled", "effort": "low" } }
    }
  }
}
```

Validation is fail-loud: an unknown rung/model key, a chain key or rung missing from
`vision`, or a chain containing its own key aborts the launch; `openai` rungs are pruned
with a warning (not a fallback target in v1 — the OAuth bearer only arrives on openai
primaries). Chain keys and rungs may carry an explicit effort suffix such as `@low` or
`@high`; those are distinct runtime rungs, while provider and `vision` validation uses the
base model key. `rungOptions` supplies exact runtime-rung options and takes precedence over
the base model catalogue options; incoming effort knobs are stripped before the selected
options are applied. A request without a detected effort variant tries `@high` first and
then the bare chain for compatibility. The proxy is transparent: agents are never told a
fallback answered, and the stderr walk lines are the user's only signal. Omitting the key
starts no proxy and leaves the emitted config byte-identical — the rollback switch.

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
viewer-agnostic. The same frames are also appended as newline-delimited JSON to the
per-tab **sidecar** file named by `$CONDASH_TRANSCRIPT_FILE` when a capturer (condash) sets
it — a reliable transport for a capturer that reads a file rather than the pty's `/dev/tty`
echo, which a TUI's controlling terminal can hide. The plugin path is appended to
`OPENCODE_CONFIG_CONTENT.plugin` (unioned
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

**Key order is preserved.** opencode evaluates a `permission` map in key order and keeps
the **last** matching rule, so order carries meaning — a trailing guard is what bounds a
permissive prefix glob:

```json
{
  "bash": {
    "*": "deny",
    "git log*": "allow",
    "*|*": "deny"
  }
}
```

Here `git log --oneline | sh` matches `git log*` and then `*|*`, and the deny wins because
it is last. agedum emits the document in authored order — including across an `extends`
chain, where a base's keys keep their position and a child's additions append after them,
so a child override is evaluated last. Until v0.53.0 the document was serialized with
sorted keys, which moved every `*…` guard ahead of the alphabetic allow-list and inverted
exactly this case.

### `agentAppend` — per-agent instruction append { #agentappend }

An agent's narrative `prompt` describes its role. Some rules are neither role description
nor `permission` — e.g. a workflow trigger like *"if asked to change a sibling repo, hand
off to the build agent"*. `agentAppend` lets those live **beside** the prompt instead of
inside it: declare it in the agent's `opencodeConfig.agent.<name>` block, next to its
`prompt`, and agedum folds it onto the **end of that agent's `prompt`** — a single blank
line between — before the config reaches opencode. The synthetic `agentAppend` key is
stripped, so opencode only ever sees one `prompt`. (It lives in the `opencodeConfig`
passthrough beside `prompt`, not in `agentOptions` — like `prompt`, which is also a
passthrough-only field.)

```json
{
  "harness": "opencode",
  "config": {
    "opencodeConfig": {
      "agent": {
        "conception": {
          "mode": "primary",
          "prompt": "You are the planning agent. Plan first, then act.",
          "agentAppend": "## Handoff rule\n\nIf asked to edit a sibling repo, hand off to the build agent — do not edit it yourself."
        }
      }
    }
  }
}
```

opencode then receives, for the `conception` agent:

```text
You are the planning agent. Plan first, then act.

## Handoff rule

If asked to edit a sibling repo, hand off to the build agent — do not edit it yourself.
```

- **String or list.** A string is appended after trimming its surrounding whitespace; a
  **list of strings** is trimmed per entry and joined with a blank line between entries (so
  each block keeps its own heading) — use it to stack several independent rules. The prompt
  and the append are always separated by exactly one blank line (surrounding whitespace is not
  preserved).
- **Heading is yours.** agedum adds no heading of its own; write the `## …` (or none) inside
  the `agentAppend` text so you control the rendering.
- **Inheritance.** Because it is an ordinary config field, `agentAppend` flows through
  [`extends`](../provider.md): a base can define it for an agent and a child inherits it. A
  child overrides it by setting its own value, or **clears** an inherited append by setting
  it to `null`. An agent with `agentAppend` but no `prompt` gets the append text as its whole
  prompt; an agent whose `prompt` is not a string is an error.
- **Per-agent, opencode-only.** It attaches to one named agent, so it is meaningful only for
  opencode — the sole harness that carries per-agent prompts in the provider config. The
  other harnesses draw their instructions from `AGENTS.md` (with the per-harness
  `AGENTS.<harness>.md` [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope)),
  which is where a shared, non-agent-specific rule belongs.

## MCP servers { #mcp }

`mcpServers` is the [canonical cross-harness vocabulary](../provider.md#mcp), translated
into opencode's own `mcp` block inside `OPENCODE_CONFIG_CONTENT`. opencode's dialect
diverges from the canonical one in three ways, all handled by the translation:

- `command` is a **single array** — the binary followed by its args.
- the stdio environment key is **`environment`**, not `env`.
- every entry carries an explicit `type` (`local` / `remote`) and `enabled: true`.

```json
"config": {
  "mcpServers": {
    "nodum":  { "command": "nodum", "args": ["mcp", "serve"],
                "env": { "NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}" } },
    "buffer": { "url": "https://mcp.buffer.com/mcp",
                "headers": { "Authorization": "Bearer ${BUFFER_KEY}" } }
  }
}
```

becomes

```json
"mcp": {
  "nodum":  { "type": "local", "command": ["nodum", "mcp", "serve"],
              "environment": { "NODUM_AGENT_TOKEN": "{env:NODUM_AGENT_TOKEN}" }, "enabled": true },
  "buffer": { "type": "remote", "url": "https://mcp.buffer.com/mcp",
              "headers": { "Authorization": "Bearer {env:BUFFER_KEY}" }, "enabled": true }
}
```

- **`${VAR}` is respelled to `{env:VAR}`**, opencode's own syntax, and never resolved — the
  token stays out of `OPENCODE_CONFIG_CONTENT` and out of `--dry-run` output. opencode
  expands it as `(config.env?.[VAR] ?? process.env[VAR]) || ""`, so name the var in
  `requiredEnv`: an unset one silently becomes the empty string and surfaces much later as
  an auth failure.
- The block is merged **before** [`opencodeConfig`](#opencodeconfig), so a launcher can
  still override a single server in opencode's own dialect (e.g. `{"mcp": {"nodum":
  {"enabled": false}}}`) without abandoning the shared base it extends.
