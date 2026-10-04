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
| `promptVars` + `opencodeConfig.agent.<name>.prompt._template` | explicit string defaults and an inline prompt template reference with optional `prompt._vars` overrides — see [below](#prompt-templates) |
| `permissionVars` + `opencodeConfig.agent.<name>.permission._template` | explicit string defaults and an inline reference to a shared permission object — see [below](#permission-templates) |
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
and secret values in `OPENCODE_CONFIG_CONTENT` are masked in `--dry-run`. Redaction acts on
parsed values before diagnostic JSON serialization, so quote/backslash/Unicode keys remain
masked without corrupting JSON, numeric fields, or unrelated prompt text. The actual launch
document uses normal JSON escaping and is not changed by diagnostic redaction.
Exact string-valued secrets, including single digits, are masked. A one-character required
environment value is not substituted inside unrelated text (such as a prompt or model id);
credential fields are masked regardless of the credential's length.

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
proxy only when valid. An absent or null block disables it; present empty objects, lists,
false, empty strings and other malformed values fail loudly, including on other harnesses.
The built-in OpenAI route recognizes the effective default model as well as native/modeled
agent model selections, so a default-only OpenAI primary needs no redundant agent declaration.

The valid block starts a local
**failover proxy** for the launch and point the routed providers' `options.baseURL` at it
(`<proxy>/oc/<id>`; the built-in `openai` provider is overlaid the same way, OAuth
untouched). The proxy forwards the primary attempt verbatim and — when it hits an
**admission wall** before any byte reached opencode (a `detect.status` code, or a 4xx whose
first 2 KB carries a `detect.messages` substring) — re-issues the request down the model's
`chains` with per-rung auth, model id, and effort options, so the session lands on a
surviving rung and opencode never sees the 429 to retry.

Every request requires a separate random per-launch capability in
`X-Agedum-Proxy-Capability`, checked in constant time before upstream contact. Agedum adds
`options.headers` entries referencing the runtime-only `AGEDUM_PROXY_CAPABILITY` env var,
including on the built-in OpenAI route. OAuth still supplies the primary bearer/account
headers; capability admission does not replace the upstream key or OAuth token. The proxy
strips capability headers on every hop and removes all case-insensitive incoming
authorization/API-key fields plus the account header before inserting the fallback's key.
The capability is never persisted in authored config, placed in argv/URLs, or printed;
temporary env values are restored on exit. Browser `Origin` is rejected as additional
defense, not the authentication mechanism. Callers already able to read the child's private
runtime environment are outside this admission boundary.

Transparent primary and untranslated fallback routes preserve the original query string,
including repeated keys and percent-encoded values. Routing uses the path alone. SSE bodies
relay available upstream data before EOF rather than waiting for a fixed-size buffer.

**openai primaries translate onto chat-completions rungs.** The OAuth/codex route speaks
the Responses API, so a Responses-shaped request (`input` present) keeps its verbatim
primary forward, but a fallback rung — a `providerDef`, always Chat Completions — receives
it translated: `instructions` becomes the system message and `input` the `messages`, the
hop targets `/chat/completions` with `Accept: text/event-stream`, and the upstream's
Chat-Completions SSE stream is relayed back as Responses SSE events
(`response.created` → reasoning/text/tool-call items → `response.completed`) through the
same translator the codex harness uses. Non-200 responses keep the wall classification and
error capture verbatim (substrate-independent), and a translated 200 pins the rung like
any other. The shared converter requires a successful finish reason and `[DONE]` for
`response.completed`: EOF/length/filter become `response.incomplete`, and upstream errors,
malformed streams or invalid tool arguments become `response.failed`. Failed/incomplete
turns never finalize tool arguments. Untranslated hops (chat primaries → chat rungs) are unchanged.
Translated Responses hops deliberately replace the source route and query with
`/chat/completions`. Like the Codex proxy, they send exactly one `Accept-Encoding: identity`;
any non-identity upstream `Content-Encoding` produces an explicit 502, not an empty
successful turn. No decompression is attempted.

Chain exhaustion returns the last
upstream error verbatim (native retry/death behaviour, never worse); image-bearing requests
walk only `vision: true` rungs — though a translated hop flattens the image away
(`input_image` parts are dropped, the rung answers on the text alone); a per-launch rung pin
skips a walled primary on later requests. `maxWalk` caps rungs tried per request. The stderr
walk lines name each wall and the rung that finally answered (a pinned replay is silent).

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
then the bare chain; failing both, it resolves to any effort-suffixed chain sharing the
model key (sorted, deterministic) — the openai Responses wire carries no effort knob at
all, so a knob-less primary still finds its model's authored chain, and a worker without
an authored chain inherits its base model's chain. The proxy is transparent: agents are
never told a fallback answered, and the stderr walk lines are the user's only signal.
Omitting the key starts no proxy and leaves the emitted config byte-identical — the
rollback switch.

#### `wait` — wait for the limit reset { #failover-wait }

The optional `wait` sub-block opts a launch into **wait-for-the-limit-reset**. Without it,
chain exhaustion returns the last upstream error verbatim (native retry/death behaviour).
With it, a *waitable wall* at **true exhaustion** — the walk reached the end of its
attempts, never a `maxWalk` cap break, and never the proxy's own unreachable-upstream 502 —
is answered with a **retryable `429 + Retry-After`** instead: opencode's own session-level
retry sleeps exactly that long and re-issues the same request through the proxy, which
re-walks from scratch. One assistant step bridges ≈5× the emitted `Retry-After` (opencode
retries 5 times per step; the budget resets each step).

```json
"failover": {
  "detect": { "status": [429, 402], "messages": ["usage limit", "quota"] },
  "chains": { "kimi-coding/k3": ["kimi-coding/k3-low"] },
  "vision": { "kimi-coding/k3": true, "kimi-coding/k3-low": true },
  "wait": { "maxWaitHours": 8, "probeSeconds": 3600 }
}
```

- `maxWaitHours` (required, finite number > 0; NaN/infinity rejected) bounds what the proxy itself emits: a forged
  `Retry-After` never exceeds it, and a wall whose own `Retry-After` exceeds it is not
  waitable (it passes verbatim). It does not cap a header-carrying wall the client
  already received — the verbatim passthrough hands the header to opencode, which honours
  it (hours-scale included).
- `probeSeconds` (optional integer > 0, default 3600) is the `Retry-After` forged when the
  wall carries no usable header: a bounded probe — the client re-issues, the proxy
  re-classifies, and a still-walled wall gets a fresh forge.
- The reset source is the wall's own `Retry-After` (delta-seconds or HTTP-date) when it
  parses and fits the cap, else `probeSeconds`. A wall that is already `429` with a usable
  `Retry-After` within the cap passes through **verbatim** — the forge would be a no-op.
  Any other waitable wall is forged: status line `429`, the captured headers replayed with
  exactly one `Retry-After` (the computed reset) replacing any captured one, `Content-Length`
  recomputed, and the **original upstream body verbatim** (the user's error display
  survives). A 402 balance wall becomes a retryable 429; a headerless 429 stops dying
  after ~65 s. Everything else — no `wait` key, a non-wall, a cap break, a reset beyond
  the cap — is the silent verbatim passthrough, byte-identical to the pre-`wait` proxy.

**The wait-only shape.** With `wait` present, `chains` becomes optional. A chainless
launcher (`chains` absent or empty) walks its primary alone: the wall reaches true
exhaustion and the forge fires, so the session waits for the model it was launched with
instead of dying. The engagement is uniform — a chains-bearing launcher's *unmapped* model
also walks its primary alone under `wait` (a model with no fallback has exactly one rung
worth waiting for); without `wait`, an unmapped model still transparent-forwards verbatim.
A chainless walk is never pinned, and after a wait-retry succeeds on the primary nothing
is pinned — the next request starts at the primary again.

**stderr lines** (same `agedum failover:` prefix as the walk lines): at the forge (and at
an already-headered 429 passing verbatim), `wait: walled at rung <index> (<rung>) —
retryable wall sent, opencode retries in <N>s`; at a non-waitable exhaustion under `wait`,
`exhausted (<rung>) — wall not waitable (reset beyond maxWaitHours), passing through`; and
on the first primary 200 after a forge, `wait cleared — primary answered after <T>s`.

**Engine floor.** Engines older than the release introducing `wait` silently drop an
authored `failoverIntent.wait` key (unknown intent keys are ignored, no error) — the
launcher runs without wait. Configs start rolling out only once the fleet is past that
floor.

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

Sidecars are created as 0600 inside owned 0700 storage directories (new parents are
also 0700). Existing foreign-owned or non-private storage, symlinks, hardlinks and
non-regular files are rejected without chmod or content writes. Linux directory
handles and no-follow opens anchor appends to the validated directory. A rejected
sidecar does not interrupt the agent; terminal OSC capture remains available. The
consumer owns retention and must provision private storage rather than relying on
the plugin to repair an unsafe existing path.

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

### Prompt templates — shared agent text { #prompt-templates }

An abstract YAML provider fragment may define a top-level `promptTemplates` mapping
of names to string templates. An OpenCode launcher includes that fragment and provides
`config.promptVars` string defaults; each agent explicitly opts in with
`config.opencodeConfig.agent.<name>.prompt._template` and may override defaults using
`prompt._vars`:

```yaml
# base/worker.yaml (included, abstract: true, schema: agedum-provider/v1)
promptTemplates:
  worker: 'You are {ID} in {POOL}. Use {{braces}} literally.'

# launcher.yaml (schema: agedum-provider/v1, harness: opencode)
include: base/worker.yaml
config:
  promptVars: {POOL: four-worker pool}
  opencodeConfig:
    agent:
      luna:
        mode: subagent
        model: openai/example
        description: Luna worker
        prompt: {_template: worker, _vars: {ID: Luna}}
        permission: {bash: deny}
        agentAppend: 'Report the result.'
```

After `include`/`extends` merging and model expansion, agedum substitutes only
explicit `{NAME}` placeholders in the selected string; `{{`/`}}` escape literal
braces. Default variables yield to per-agent variables. There is no nested
templating, arbitrary YAML interpolation, file reference, implicit variable
inference, or template expansion inside variable values. Missing templates,
missing placeholders, malformed placeholders (including positional, attribute,
index, conversion, and format syntax), non-string values, malformed `_template`,
`_vars` without `_template`, and extra keys inside a templated `prompt` fail with
the agent name. Legacy agent-level `promptTemplate`/`promptVars` remain accepted,
but mixing either with nested `prompt` metadata on the same agent is an error. A
legacy template reference alongside a literal `prompt` is also ambiguous. Keep permissions,
mode, model, and description in the launcher agent entry: templates supply text
only. Ordinary prompts and JSON providers without template fields keep their
existing behavior.

Both `--print-config` and launch use the same non-mutating resolver; neither emits
`promptTemplates` or `promptVars`. Print shows the rendered base `prompt` and leaves
`agentAppend` separate. The launch builder then folds that append with one blank
line and strips it, so OpenCode receives a plain final `prompt`.

### Permission templates — shared tool actions { #permission-templates }

An abstract included OpenCode fragment may define top-level `permissionTemplates`:
each name maps to a permission object containing shared tool actions, **not** an agent
object. The template must not contain `task`. Each agent opts in with
`permission._template`, optionally overrides defaults with `permission._vars`, and may author
only a literal `permission.task` rule map alongside them; agents without a local task map
inherit OpenCode's normal task behavior. Non-templated agents retain literal permissions,
and `opencodeConfig.permission` is untouched.

```yaml
# base/permissions.yaml (abstract included fragment)
permissionTemplates:
  worker:
    read: allow
    question: '{QUESTION}'
    bash:
      '*': deny
      'git log*': allow
      '*|*': deny

# launcher.yaml (harness: opencode, includes base/permissions.yaml)
config:
  permissionVars: {QUESTION: deny}
  opencodeConfig:
    agent:
      worker:
        permission:
          _template: worker
          task:
            '*': deny
            worker: allow
      primary:
        permission:
          _template: worker
          _vars: {QUESTION: allow}
          task:
            '*': deny
            worker: allow
```

Values in `config.permissionVars` and agent `permission._vars` are **strings only**; agent values override launcher defaults
as whole values. In a template value, only an entire `{NAME}` scalar is replaced,
**once**. Embedded braces (`echo {NAME}`), unmatched braces, and shell patterns are
literal; an entire but malformed `{...}` reference errors. No keys are interpolated,
and replacements are not parsed again. Template actions are strings or one-level ordered
rule maps with string keys and string leaves (empty rule maps are accepted); nested maps,
lists, non-string variables, missing variables, unknown templates, and `permission._vars`
without `permission._template` fail loudly. Unused variables are validated too. With a
template, any literal `permission` key other than `_template`, `_vars`, and `task`, or a `task` that
is not an ordered rule map, is rejected. The template and the literal task map never
compete for a key. There is no inferred worker allow-list and no arbitrary permission
merge. The deprecated agent-level `permissionTemplate`/`permissionVars` spelling remains
accepted with the same rendering, but mixing either legacy field with inline metadata on
one agent is an error. The resolver checks this bounded shape, not OpenCode's entire
permission DSL.

Resolution runs after include/extends and model expansion for both `--print-config` and
direct launch, without mutating its input. Synthetic template and variable fields are
removed from the effective document; the rendered permission map preserves rule order,
including the trailing `bash` guard and the first `task` deny. **Source ownership is not
enforced here**: include/extends deep-merges literal agent `permission.task` maps *before*
this resolver, potentially retaining an inherited allow. A launcher fleet requiring
source-local task lists must check its raw source and shared fragments separately; an
effective-roster comparison alone cannot establish provenance.

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
