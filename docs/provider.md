---
title: Provider mode · agedum
description: Launch a harness from a provider config (JSON or YAML) — agedum resolves the provider's env from a .env, validates the required vars, sets the provider/model/auth environment, and launches the harness inside the virtual-file context.
---

# Provider mode

```text
agedum <provider-name|config.json|.yaml> [--env <file>] [--dry-run] [--print-config] [harness args...]
```

Provider mode is the **normal way to launch** an agent. agedum reads a **provider config**
(JSON, or YAML declaring `schema: agedum-provider/v1` or `/v2`), resolves the provider's
secrets from a
`.env`, sets the provider/model/auth environment, and launches the harness named in the config
— all in one process, inside the same virtual-file context [wrapper mode](wrapper.md) uses.
There is no generated launcher script: the config is read at run time.

```bash
agedum claude-deepseek-auto                   # resolve the named provider, launch claude
agedum claude-deepseek-auto -p "review this"  # extra args go to the harness
agedum ./providers/my-claude.json             # a path instead of a name
agedum claude-deepseek-auto --dry-run         # print the resolved env + argv, don't launch
agedum claude-deepseek-auto --print-config    # print the effective config as YAML, exit
```

This page covers the mechanism shared by every provider: how the config and env resolve,
the config envelope, prompt-seeding, and `--dry-run`. The **`config` block is per-harness**
— each harness page has a working recipe and the full key mapping:

| Harness | Provider config |
|---|---|
| Claude | [recipe + mapping](harnesses/claude.md#provider-config) |
| kimi | [recipe + mapping](harnesses/kimi.md#provider-config) |
| opencode | [recipe + mapping](harnesses/opencode.md#provider-config) |
| Cline | [recipe + mapping](harnesses/cline.md#provider-config) |
| reasonix | [recipe + mapping](harnesses/reasonix.md#provider-config) |
| aider | [recipe + mapping](harnesses/aider.md#provider-config) |
| pi | [recipe + mapping](harnesses/pi.md#provider-config) |
| codex | [recipe + mapping](harnesses/codex.md#provider-config) |

## Resolving the provider

The single positional argument is a **config reference**, resolved **relative to the
providers root** (`${AGENTS_PROVIDERS_DIR:-~/.config/agents/providers}`):

- a value starting with `/` is an **absolute** filesystem path;
- anything else is **relative to the providers root** — nested paths included, so configs may
  be organised in subdirectories: `agedum claude/deepseek.json` → `<root>/claude/deepseek.json`;
- a value with **no recognised extension** tries `.json` first, then `.yaml`, then `.yml`
  (`agedum claude/deepseek` also works);
- an explicit **`.json` reference that does not exist falls back to its `.yaml` sibling** —
  so a base converted from JSON to YAML keeps every old referrer working, and
  **`.yaml` / `.yml` references resolve as-is**;
- a reference that resolves to no file is an **error** — there is no CWD or fallback search.

Run [`agedum --providers`](cli.md#listing-providers) to list the launchable configs by their
path (e.g. `claude/deepseek`). A config's **identity and label are its path** — there is no
`name` field.

Any token after the provider that isn't an agedum flag is passed to the harness verbatim
(`agedum claude-deepseek-auto -p "hi"` runs `claude -p "hi"`). `--env`, `--dry-run`, and
`--print-config` are
agedum's own flags and may appear **before or after** the provider; to forward a literal
`--dry-run`/`--env` to the harness, put it after a `--`
(`agedum claude-deepseek-auto -- --dry-run`).

## The env file

Secrets are read from `${AGENTS_ENV_FILE:-~/.config/agents/.env}`, overridable per-run with
`--env <file>`. It is a simple `KEY=VALUE` file (an optional `export ` prefix and
surrounding quotes are honoured; `#` lines and blanks are skipped). Every variable named in
the config's `requiredEnv` (plus `secretEnv`) must be present and non-empty, or agedum
fails fast with a clear message before launching.

Unlike the retired `--build-script` codegen — which emitted a wrapper that sourced the
`.env` itself, so agedum never saw a token — provider mode reads the env file into the
agedum process and sets the resolved values in the child environment.

## Config shape

The config is the condash-style agent envelope:

```json
{
  "harness": "claude",
  "secretEnv": "DEEPSEEK_API_KEY",
  "requiredEnv": ["DEEPSEEK_API_KEY"],
  "config": { "...": "per-harness options" }
}
```

| Field | Meaning |
|---|---|
| `harness` | `claude`, `kimi`, `opencode`, `cline`, `reasonix`, `aider`, `pi`, or `codex`. Selects the translation **and** the harness to launch; read from the file (there is no `--harness` flag). |
| `secretEnv` | The env var holding the API token. Per harness: `claude` maps it to `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`; `kimi` / `opencode` / `reasonix` / `aider` / `pi` / `codex` pass it through under its own name (reasonix reads it via the provider's `api_key_env`, aider via litellm, pi by the conventional var name or a `$VAR` reference in a generated `models.json`, codex by the provider's `env_key`); `cline` passes it as `--key`. |
| `requiredEnv` | Vars validated and exported into the child. `secretEnv` is always appended if not listed. Declare a provider's API-key var here so a harness that reads it from the environment sees it. |
| `config` | The per-harness option block — see the harness page table above. |
| `extends` | Optional — a config reference **or list** of them; the named base(s) are deep-merged and this config's keys applied last. See [Extending configs](#extends). |
| `include` | Optional — a config reference **or list** of them; shared fragments pasted into this config (composition, not inheritance). See [Including fragments](#include). |
| `abstract` | `true` marks a **base-only** config: excluded from `--providers` and not launchable on its own. |
| `sandbox` | Optional **write-confinement** — mount the host read-only and let the harness write only to the project root, its own state/config dir (e.g. `~/.cline`), `/tmp`, and the paths in `sandbox.readWrite`. See [Filesystem sandbox](#sandbox). |

A config's **identity and label are its path** under the providers root — there is no `name`
field. Save the config at the path you want to launch it by (e.g.
`~/.config/agents/providers/claude/deepseek.json`), put the API token in
`~/.config/agents/.env`, then `agedum claude/deepseek.json --dry-run` to check it.

## YAML configs { #yaml }

A config may also be a **YAML document** (`.yaml` or `.yml`). YAML configs are **versioned**:
the document must declare the envelope version in a top-level `schema` key:

```yaml
# providers/claude/deepseek.yaml
schema: agedum-provider/v1
harness: claude
secretEnv: DEEPSEEK_API_KEY
requiredEnv:
  - DEEPSEEK_API_KEY
config:
  baseUrl: https://api.deepseek.com/anthropic
  model: deepseek-v4-pro
```

- `schema: agedum-provider/v1` is **required**. A missing or different value fails the load
  with an error naming the expected version.
- A valid config yields **exactly the same envelope the equivalent JSON would** — the
  `schema` key is consumed by the loader, and everything downstream (extends merging, the
  per-harness mapping, `--dry-run`) is identical. agedum *parses* YAML; it never converts
  files, and JSON stays a permanent second input format.
- JSON configs need no `schema` key and load exactly as they always have — the version key is
  a YAML-only requirement.
- A YAML config may also declare **`schema: agedum-provider/v2`**, the
  [effort-carrier expansion](#expansion) opt-in. JSON documents carry v1 semantics
  forever: v2 is YAML-only.

### The YAML boolean trap { #yaml-boolean-trap }

YAML 1.1 reads an unquoted `on` / `off` / `yes` / `no` as a **boolean**, not a word. In a
config that is almost never what you mean — `secretEnv: on`, `compaction: off`, or an env
value `extraEnv: {FOO: no}` would reach the harness as `"True"`/`"False"`. agedum therefore
**rejects** an unquoted boolean in any string-valued slot (env values, `secretEnv` /
`requiredEnv` entries, `extends` references, `harness`, and the string keys of the `config`
block such as `model`, `baseUrl`, `compaction`) with a named error:

```text
yaml boolean trap at config.extraEnv.FOO: unquoted on/off/yes/no parsed as boolean — quote the value
```

Quote the value (`secretEnv: "on"`) and it passes through verbatim. Booleans that *are*
booleans — `abstract: true`, `foldSystemMessages: true`, kimi's `thinking`, codex's
`codexConfig` flags — are untouched. `${VAR}` placeholders are never interpolated by the YAML
reader; they survive verbatim exactly as in JSON.

## Extending configs — `extends` { #extends }

A config can **`extends`** one or more **base** configs and inherit their settings, so shared
options are written once. `extends` is a config reference (or a list of them), resolved the
same way as the launch argument — relative to the providers root, or absolute when starting
with `/`:

```json
// providers/base/claude-deepseek.json   (a shared base, not launched directly)
{ "abstract": true, "harness": "claude", "secretEnv": "DEEPSEEK_API_KEY",
  "config": { "baseUrl": "https://api.deepseek.com/anthropic", "effortLevel": "max" } }

// providers/claude/deepseek.json
{ "extends": "base/claude-deepseek.json", "config": { "model": "deepseek-v4-pro" } }
```

`agedum claude/deepseek.json` then launches the **merged** config (base + child). Rules:

- **Merge** is a deep-merge: nested objects (like `config`) combine key-by-key. With a **list**
  of bases, they merge left→right and the extending config is applied **last** (child wins).
- **`requiredEnv` unions** instead of being replaced — it is the one exception to child-wins.
  Lists otherwise replace wholesale, which would mean a child declaring its own
  `requiredEnv` silently drops the base's: the base's var would go unvalidated and
  unexported, and whatever it configured (an [MCP](#mcp) `${VAR}`, a provider key) would
  fail at first use rather than at launch. Requirements accumulate down the chain — base
  order first, the child's additions appended, duplicates dropped.
- **Recursive** — a base may itself `extends` another.
- **Formats mix freely** — each reference resolves by the rules above, so a YAML child may
  extend a JSON base and vice versa (an explicit `base/x.json` reference even keeps working
  after `x` is converted to YAML, via the [`.yaml` sibling fallback](#yaml)).
- A **cycle** (a → b → a) or a base that resolves to no file is an **error**.
- `abstract: true` marks a config as a base only: it is skipped by `--providers` and refuses to
  launch directly (`agedum base/claude-deepseek.json` errors). Abstractness is **not** inherited
  — a config that extends an abstract base is itself launchable.

## Including fragments — `include` { #include }

`extends` expresses two different relationships today: real inheritance (a child specialising a
base) and plain fragment sharing (a config extending a base just to paste in its MCP block).
`include` is the second, said directly: it **pastes a shared fragment in** without making it a
prototype. Like `extends`, it takes a config reference or a list of them, resolved by the same
[rules](#resolving-the-provider) (`.json`/`.yaml` fallback included), and every reference of
either kind may point at either format.

The **merge order** for one config is, most-default first: every `include` target (each
recursively resolved, its own `include`/`extends` already applied), deep-merged left→right —
earlier include is the more default; then the `extends` chain, whose keys beat an included
fragment's on conflict (inheritance overrides composition); then the file's own keys —
`requiredEnv` unions across all three layers:

```json
// providers/base/mcp-nodum.json   (a shared fragment, not a prototype)
{ "abstract": true, "requiredEnv": ["NODUM_AGENT_TOKEN"],
  "config": { "mcpServers": { "nodum": { "command": "nodum", "args": ["mcp", "serve"] } } } }

// providers/claude/opus.json
{ "include": "base/mcp-nodum.json", "harness": "claude",
  "config": { "settings": { "model": "opus" } } }
```

Rules:

- **Composition only** — an included fragment is not a prototype: `abstract` is **not**
  inherited through an include, and an included target is not "applied" in any launch sense.
  Give fragments `abstract: true` (as above) so they stay out of `--providers` and refuse a
  direct launch, exactly like bases.
- **`include` is a meta key** like `extends` — consumed during resolution, never present in
  the merged result.
- **Cycles are detected across the combined include+extends graph**: a file included twice
  through different paths is fine (a DAG merge — it simply merges twice, idempotently), but a
  cycle (`a` includes `b`, `b` includes `a`; or `a` extends `b`, `b` includes `a`) is an error,
  as is a reference that resolves to no file or a target that parses to a non-mapping.
- **All the per-file checks apply to fragments too** — a YAML fragment's `schema` key and
  [boolean-trap](#yaml-boolean-trap) walk run on the fragment's own file, so a trap inside an
  included fragment names that fragment's path.
- `--dry-run` output is unchanged: composition detail is not reported, the effective result is.

## Model catalogue — `models.yaml` + `modelRef` { #model-catalogue }

A hand-written opencode launcher that defines a custom provider must inline a catalog block
per model (the model's `name`, `limit`, `modalities`, `attachment` — the block opencode itself
consumes) under `config.opencodeConfig.provider.<id>.models`. When several launchers serve the
same models, that block is duplicated in each. The **model catalogue** is a shared,
run-time source for those fragments: a `models.yaml` at the providers root, and a `modelRef`
on a `config.providerDef` entry that pulls one entry in at launch.

```yaml
# providers/models.yaml   (fixed filename at the providers root)
schema: agedum-models/v1
models:
  deepseek-v4-pro:
    name: DeepSeek V4 Pro
    limit:
      context: 1000000
      output: 65536
  deepseek-flash:
    name: DeepSeek V4.1 Flash
    attachment: true
    limit:
      context: 1000000
      output: 65536
    modalities:
      input: [text, image]
      output: [text]
```

```yaml
# providers/oc/hand-ds.yaml — the catalog block comes from the catalogue
schema: agedum-provider/v1
harness: opencode
secretEnv: DEEPSEEK_API_KEY
config:
  model: deepseek/deepseek-v4-pro
  providerDef:
    id: deepseek
    npm: "@ai-sdk/openai-compatible"
    baseUrl: https://api.deepseek.com
    apiKeyEnv: DEEPSEEK_API_KEY
    modelRef: deepseek-v4-pro      # a catalogue id, or a list of them
```

At launch — after `include`/`extends` merging, before the launch builds — each `modelRef`
expands to the catalogue entry filed under
`config.opencodeConfig.provider.<providerDef id>.models.<id>`, exactly where the generated
`oc/*.json` configs carry their catalog inline; an equivalent config with the block written by
hand merges to the same result, and `--dry-run` shows the expanded form. The entry vocabulary
is **opencode's own** — whatever opencode consumes per model (`variants`, `options`, …) can
live in the catalogue and rides through verbatim.

Rules:

- **Opt-in, zero-change otherwise** — a config with no `modelRef` anywhere and no
  `modelsCatalog` never touches the catalogue (which need not exist), and the generated
  `oc/*.json` configs are unaffected.
- **The catalogue is data, not a config**: `--providers` skips the fixed root-level
  `models.yaml` (only that exact filename — a `models.yaml` in a subdirectory stays an
  ordinary config candidate).
- **Errors are named**: a catalogue missing, carrying a wrong `schema:` value, or holding a
  malformed entry (checked minimally: `name` string, `attachment` boolean, `limit` integers
  or null, `modalities` string lists — errors name the model id and key) raises a
  `ProviderError` naming the catalogue path; a `modelRef` missing from the catalogue names
  the ref, the providerDef, and the path. A config carrying `modelRef` on any harness other
  than opencode is refused with a message naming the harness (not the ref or path).
- **Override the location** with a top-level `modelsCatalog: <ref>` (resolved like
  `include`; YAML-only — a non-`.yaml` ref is an error). It is a meta key like
  `extends`/`include`, consumed and stripped, and a declared pointer loads and validates
  even when nothing references it.
- **No YAML boolean-trap walk on the catalogue in v1** (a documented limit): the catalogue's
  per-model entries are passed through verbatim, so quote any value that reads as
  `on`/`off`/`yes`/`no` yourself.
- **opencode first** — claude and codex need no expansion today; a `modelRef` on their
  configs fails loudly rather than being ignored.
- **`carrierMeta`** — the catalogue may carry an optional top-level facts section read by
  [v2 expansion](#expansion); `modelRef` filing never reads it, so a catalogue with
  `carrierMeta` files byte-identically on every engine ≥ 0.60 (older engines ignore the
  section entirely — the schema stays `agedum-models/v1`).

## Expansion — `agedum-provider/v2` { #expansion }

`schema: agedum-provider/v2` is the **effort-carrier expansion opt-in**. A v2 document is
byte-shaped exactly like a v1 document — same envelope, same authored policy blocks
(prompts, permissions, `providerDef`, `requiredEnv` all stay authored verbatim) — with
these deltas:

1. `schema: agedum-provider/v2` (the opt-in itself);
2. agent entries and `config.model` may declare **`model: <catalogue-key>@<effort>`** refs;
3. an optional top-level **`expansionModels:`** list of the same refs declares universe
   members with no agent entry (failover-only models and efforts). It is consumed like
   `modelsCatalog` — stripped before the launch;
4. an optional top-level **`failoverIntent:`** block declares run-time failover intent
   ([derived into the effective `failover` block](#failover-intent) from the config's own
   agents). It is consumed like `expansionModels` — stripped before the launch.

The engine derives everything carrier-specific; the author writes no `variant`,
no `options.reasoningEffort`, no alias plumbing:

```yaml
schema: agedum-provider/v2
harness: opencode
config:
  model: ds-flash@high                # an @-ref — translated by the same rule as agents
  opencodeConfig:
    agent:
      ds-flash-high:
        mode: subagent
        model: ds-flash@high          # intent: derived options.reasoningEffort: high
      ds-flash-low:
        mode: subagent
        model: ds-flash@low
  providerDef:                        # authored plumbing — never invented by the engine
  - id: ds
    npm: "@ai-sdk/openai-compatible"
    baseUrl: https://api.deepseek.com
    apiKeyEnv: DEEPSEEK_API_KEY
expansionModels: [k3@low]             # universe member with no agent entry
```

### When expansion runs

- The **root document's** declared schema gates it: a v1 base under a v2 root expands, a
  v2 base under a v1 root does not (per-file checks are unchanged — any file in an
  `extends`/`include` chain may declare either version).
- It runs after [`modelRef`](#model-catalogue) filing, before the launch builds — so
  `--dry-run` and [`--print-config`](#print-config) show the effective result. Derived
  catalog entries merge **under** what `modelRef` filed or the author wrote inline
  (authored wins on conflict, the same rule `modelRef` applies).
- JSON documents are v1 semantics forever: v2 is **YAML-only**.
- **A v1 document carrying intent is a named load error.** An opencode `config.model` or
  agent `model` string containing `@` — or a top-level `expansionModels` or
  `failoverIntent` — in a v1 document fails the launch with *"this config looks like
  expansion intent …; declare `schema: agedum-provider/v2`"*, instead of launching a
  garbage model ref that fails far from the cause. No success path changes: intent slots
  only.
- **Non-opencode harnesses**: a v2 config with intent markers on any harness other than
  opencode is refused (the `modelRef` opencode-first rule). A v2 config with **no**
  markers (`@`-refs, `expansionModels`, and `failoverIntent` all absent) is a no-op —
  declaring v2 alone is not intent.
- A precomputed **`failover`** block passes through untouched; a v2 document carrying
  both it and `failoverIntent` is a named expansion error (two declarations of the same
  block — the engine refuses to guess which wins). See [Failover](#failover-intent).

### `carrierMeta` — the catalogue's facts section

Expansion resolves refs against the [model catalogue](#model-catalogue) extended with an
optional top-level **`carrierMeta`** section — per-model facts, separate from the verbatim
catalog fragments:

```yaml
# providers/models.yaml (continued)
carrierMeta:
  ds-flash:
    provider: ds            # provider id catalog entries file under
    family: deepseek        # selects the effort carrier
    efforts: [high, low]    # what the model accepts
    display: DS Flash       # display-name fact (may differ from the fragment's name)
    vision: true            # failover vision-map fact (read by `failoverIntent` expansion)
  k3:
    provider: kimi-coding
    family: kimi
    efforts: [high, low]
    display: Kimi K3
    aliases: {high: k3, low: k3-low}   # model-alias families only
    alias_model_id: k3                 # required iff `aliases`
    vision: true
```

Entries are validated whenever the catalogue loads (named `ModelCatalogSchemaError`s):
`provider`/`family`/`display` non-empty strings, `efforts` a non-empty list inside the
effort alphabet (`high`, `low`), `aliases` a mapping of alphabet efforts to non-empty
strings, `alias_model_id` required iff `aliases` is present. Unknown keys inside an entry
are ignored — future facts land here without a catalogue change. `vision` (a boolean) is
one of those permissive facts: read only by [`failoverIntent`](#failover-intent)
expansion, ignored everywhere else. `modelRef` filing never reads the section, so the
`models` map is byte-identical on every engine ≥ 0.60 (older engines ignore `carrierMeta`
entirely).

### Per-carrier semantics

The **universe** is every `@`-ref in `config.model` and the `opencodeConfig.agent` model
fields, plus `expansionModels`. Providers file their catalog entries in
**first-appearance order** of their models' refs (agent entry order, then
`expansionModels`) — no order constant lives in the engine. `config.model` `@`-refs are
translated by the same ref function (authoring sugar); plain `provider/model` strings
pass through untouched.

| Family | `model: key@effort` derives | Catalog entry (filed under `provider.<provider>.models`) |
|---|---|---|
| deepseek, glm | `model: provider/key` + `options.reasoningEffort: effort` (merged into an authored `options` map) | the fragment, verbatim |
| gpt | `model: provider/key` + `variant: effort` | the fragment + `variants:` disabling every OpenCode variant the config does **not** declare (vocabulary order; declaring `sol@high` and `sol@low` anywhere in the config keeps `low` enabled) |
| kimi | `model: provider/<aliases[effort]>` — the agent entry carries **no** carrier fields | one alias-keyed entry per declared effort (canonical `high`-first order), each `options.thinking: {type: enabled, effort}`; the `low` entry is rebuilt as `{id: alias_model_id, name: "<display> (low thinking)", …}` |

### Failover — `failoverIntent` { #failover-intent }

A v2 config may author its failover as intent instead of a precomputed block — the same
shape the manifest template uses, with refs in the `key@effort` grammar:

```yaml
schema: agedum-provider/v2
harness: opencode
failoverIntent:
  detect:                    # authored data — copied verbatim, never interpreted
    status: [429, 402]
    messages: [usage limit, quota, rate limit]
  maxWalk: 3
  chains:                    # explicit key@effort refs against catalogue keys
    sol@low: [glm-flash@high]
    terra@low: [glm-flash@high]
```

Expansion derives — from the config's **own agents** — the same top-level `failover`
block a builder precomputes, then strips the intent (consumed, like `expansionModels`):

- **Roster** — `mode: primary` agents are the mains, `mode: subagent` the workers, any
  other or absent mode is in neither; an agent whose `model` is a plain `provider/model`
  string contributes no pair. The pair, not the agent id, is the roster unit.
- **Every intent ref resolves loud first** — a ref that does not parse or names an
  unknown key/effort/model is a named expansion error *before* any filtering (resolution
  errors are authoring errors; they never depend on what the filter would later do).
- **Filtering** — a chain whose source is outside mains ∪ workers drops; a chain left
  without rungs drops; zero surviving chains omit the whole block — no `failover` key in
  the effective config. Absence means ignore, never an error: a hand config may inherit
  an intent whose chains all drop and launch without failover.
- **Universe** — the surviving chains' rung refs join the expansion universe after
  `expansionModels`, in authored order; dropped chains contribute nothing to filing. A
  rung-only model therefore files with no `expansionModels` (the key is subsumed in any
  failover-bearing config; the two keys may also be combined — the universe is their
  union).
- **Translation** — rung and source refs translate per carrier: `provider/key@effort`
  (variant / reasoningEffort families), the bare `provider/<aliases[effort]>` (kimi).
  Two surviving sources translating to the same runtime ref would be a named error.
- **`rungOptions`** — one `{"reasoning_effort": effort}` entry (snake_case, exactly as
  the builder emits) per used variant/reasoningEffort rung, canonical effort order.
  Model-alias rungs never appear.
- **`vision`** — derived from the catalogue's `carrierMeta.vision` facts: one entry per
  universe model, walked provider-major in first-appearance order; model-alias models
  additionally get one entry per declared effort at `provider/<aliases[effort]>`. A
  universe model whose `carrierMeta` lacks the fact — or carries a non-boolean one —
  is a named error — but only when a
  block is actually derived (an omitted block demands no vision facts).

`detect`/`maxWalk` are authored data copied verbatim, never interpreted or validated at
expansion — launch-time validation polices the emitted block for derived and precomputed
blocks alike. The engine filters and omits; it never enforces that a config's intent
names its roster (authoring-side roster invariants stay builder-side).

The key **collision rules**, per root schema:

| root | keys present | behaviour |
|---|---|---|
| v1 | `failover` | passthrough — unchanged |
| v1 | `failoverIntent` | the named intent load error (declare v2) |
| v2 | `failoverIntent` only | intent expands; key stripped; derived block emitted |
| v2 | `failoverIntent` + `failover` | named expansion error — two declarations of the same block |
| v2 | `failover` only | passthrough untouched |
| either | neither | nothing — the omission rule |

### Errors are named

A ref that cannot resolve is an `ExpansionError` naming where it sits: unknown catalogue
key; effort outside the alphabet (`high`/`low`); effort outside the model's `efforts`;
a referenced model with no `carrierMeta` entry; a family with no effort carrier; a
model-alias model without `aliases`/`alias_model_id`; a ref effort missing from
`aliases`; an agent entry that *authors* a carrier field (`variant` or
`options.reasoningEffort`) on a model whose ref also carries `@` (the engine refuses to
guess); `@`-refs on a non-opencode harness. One authoring trap gets its own message: a
**bare catalogue key** in `config.model` (`model: ds-flash` — no `@`, no `/`) is not a
ref and is not silently passed through; declare the effort or write the plain
`provider/model` form. `expansionModels` must be a list of `key@effort` strings.
`failoverIntent` adds its own named errors: a malformed block or chains shape; a
universe model whose `carrierMeta` has no `vision` fact (when a block is derived); a v2
document declaring both `failoverIntent` and a precomputed `failover` block.

## `--print-config` { #print-config }

Prints the **effective merged+expanded config** as YAML — `include`/`extends` resolved,
`modelRef` filed, v2 intent expanded — and exits 0. No env resolution, no
`requiredEnv` validation, no launch; the config document is the whole output, so the flag
works without an env file:

```bash
agedum oxa --print-config            # flag before or after the provider, like --dry-run
```

This is the debug/parity view of exactly what a launch would see (`--dry-run` shows the
same effective config inside the full launch view). Note `--print-config` prints the
*config*; env-var references (`apiKeyEnv`, `requiredEnv`, `${VAR}` placeholders) appear
as authored, never resolved.

## MCP servers — `config.mcpServers` { #mcp }

`config.mcpServers` declares MCP servers in **one canonical vocabulary** that agedum
translates into each harness's own dialect, so a server is written once and extended onto
every launcher that should carry it. Supported by **claude**, **opencode**, and **kimi**
(kimi with the caveat below); other harnesses ignore the key.

An entry is either **stdio** or **remote** — never both:

```json
"mcpServers": {
  "nodum":  { "command": "nodum", "args": ["mcp", "serve"],
              "env": { "NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}" }, "cwd": "/opt/x" },
  "buffer": { "url": "https://mcp.buffer.com/mcp", "transport": "http",
              "headers": { "Authorization": "Bearer ${BUFFER_KEY}" } }
}
```

| Canonical | claude | opencode |
|---|---|---|
| stdio `{command, args, env, cwd}` | passed through as-is (claude's dialect *is* the canonical one) | `{type:"local", command:[command, …args], environment, cwd, enabled:true}` |
| remote `{url, headers, transport}` | `{type: transport\|"http", url, headers}` | `{type:"remote", url, headers, enabled:true}` |
| delivery | `--mcp-config '<json>'` appended to argv | merged into `OPENCODE_CONFIG_CONTENT` |

### `${VAR}` placeholders

Any value may carry a `${VAR}` placeholder. **agedum respells it, it never resolves it** —
resolving would bake the secret into argv (claude) or into an env var (opencode), where the
process list and `--dry-run` would expose it. Instead each harness expands it itself:
claude reads `${VAR}` natively, and opencode's spelling `{env:VAR}` is written for it.

The variable still has to *reach* the harness, which means naming it in **`requiredEnv`** —
that is what copies it out of `~/.config/agents/.env` into the child environment. Declaring
it there also makes a missing token fail at launch rather than at first tool call:

```json
{ "abstract": true,
  "requiredEnv": ["NODUM_AGENT_TOKEN"],
  "config": { "mcpServers": { "nodum": { "command": "nodum", "args": ["mcp", "serve"],
              "env": { "NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}" } } } } }
```

Extend that base from any claude or opencode launcher and both the server and its env
requirement come along.

### Per-harness notes

- **claude** — `--strict-mcp-config` is deliberately *not* passed, so provider-declared
  servers are **additive** to the user's own (`~/.claude/settings.json`, project `.mcp.json`),
  not a replacement.
- **opencode** — the translated block is merged **before** `opencodeConfig`, so an explicit
  `opencodeConfig.mcp` entry still overrides one server without abandoning the shared base.
  Note opencode resolves an unset `{env:VAR}` to the empty string, which is a second reason
  to declare the var in `requiredEnv`.
- **kimi** — keeps the older verbatim passthrough into `mcp.json` ([kimi § MCP](harnesses/kimi.md#mcp)),
  and its remote form is `{url, bearerTokenEnvVar}` rather than `headers`. Kimi Code is not
  known to expand `${VAR}` there, so a placeholder in a kimi `mcpServers` entry is a
  **fail-loud error** rather than a literal handed to the server.

## Filesystem sandbox — `sandbox` { #sandbox }

An optional top-level `sandbox` block confines what the launched harness can **write**.
Without it, the harness shares your whole filesystem read-write — the namespace isolates only
*what the harness reads as config*, not where it can write. With it, the host is mounted
**read-only** and the harness can write only to the **launch directory** (the current dir —
the working tree it was launched in; the walked-up project root is used for *finding* sources
and injection targets, never as the writable grant, so a launch from a home subdir cannot
mount the whole home writable), its own **state/config dir** (agedum knows each harness's dir —
`~/.claude`, `~/.cline`, `~/.codex`, … — and always
makes it writable so the harness can persist sessions/settings/auth), a private `/tmp`, and each
path in **`readWrite`**:

```json
{
  "harness": "claude",
  "slug": "claude-boxed",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": { "...": "..." },
  "sandbox": {
    "readWrite": ["~/notes", "${PROJECT_ROOT}/build"]
  }
}
```

`readWrite` paths are templates resolved at launch: `~` → your home, `$VAR` → the
environment, `${PROJECT_ROOT}` → the launch directory. An entry holding a shell glob
(`*`, `?`, `[…]`) is expanded against the filesystem and every existing match is added — so
`~/src/*` makes each immediate subdirectory of `~/src` writable (the `~/src` dir itself is not
bound, and an unmatched glob adds nothing). A path already inside the launch directory is
redundant (the launch dir is always writable) and folded in. An empty `"sandbox": {}` still
confines — only the always-writable set applies. This is the provider-mode equivalent of
wrapper mode's [`--sandbox` / `--rw-dir`](wrapper.md#sandbox); `agedum <name> --dry-run` lists
the resulting writable set under a `sandbox · write-confinement` heading. It confines the
**filesystem** only — the network is untouched, so the harness still reaches its endpoint.
Linux-only, like the rest of the launch.

## Seeding an initial prompt — `--prompt` / `--run` { #prompt-seeding }

Two agedum flags seed the launched harness with a first prompt, abstracting over each
harness's own prompt syntax (mutually exclusive, each given once):

- **`--prompt "<text>"`** — launch **interactively** with `<text>` as the first message;
  the session stays open.
- **`--run "<text>"`** — run `<text>` **non-interactively** and exit. The form for scripts
  and tasks.

agedum maps the flag to the harness named in the config:

| Harness | `--prompt` (interactive) | `--run` (non-interactive) |
|---|---|---|
| claude | positional prompt: `claude "<text>"` | `claude --print "<text>"` |
| kimi | *(unsupported — fail-loud)* | `kimi --prompt "<text>"` |
| opencode | `opencode --prompt "<text>"` | `opencode run "<text>"` |
| cline | `cline --tui "<text>"` | `cline "<text>"` |
| reasonix | *(unsupported — fail-loud)* | `reasonix run "<text>"` |
| aider | *(unsupported — fail-loud)* | `aider --message "<text>"` |
| pi | positional prompt: `pi "<text>"` | `pi --print "<text>"` |
| codex | positional prompt: `codex "<text>"` | `codex exec "<text>"` |

For cline the prompt is a positional argument either way; `--tui` is what opens the
interactive TUI (seeded with the prompt), and a bare positional runs the task once in act
mode and exits. For kimi, reasonix, and aider only `--run` is supported — Kimi Code's
`--prompt` runs once and exits (and cannot combine with `--yolo`/`--auto`, which `--run`
therefore drops), reasonix's `run` subcommand and aider's `--message` each take the task and
exit, but none has an interactive prompt-seed (reasonix's `chat` can't be pre-seeded; aider's
`--message` exits), so `--prompt` is a fail-loud `ProviderError` (condash then falls back to
spawn-and-type for an interactive seed).

A harness with no known prompt-seeding convention is a fail-loud `ProviderError` (agedum
never guesses). Harness passthrough args are preserved, before the prompt text.

Because `--run` is non-interactive, agedum runs the harness with **`/dev/null` for stdin**
so it can never block on input it will never receive — notably `opencode run`, which hangs
forever on an open, non-tty stdin (a pipe, a headless task runner). `--prompt` keeps the
inherited stdin for the live session.

```bash
agedum claude-deepseek-auto --run "review this" --dry-run
#   command
#     claude --print review this
```

## `--dry-run` { #dry-run }

Prints the full resolved launch without running it, so you can see exactly what context the
harness is given. It names the config's **source format** (`source     yaml` / `json`) and is
grouped by **scope** (project / global); under each, every source
(`AGENTS.md`, `.agents/skills/`) is listed with its **disposition**: `→ <dest>` when
injected, `read in place` when the harness reads it natively, or an explicit note when a
scope contributes nothing. Project-scope
paths display relative to the cwd; global-scope stays `~`-absolute. The resolved config (env
vars; opencode's `OPENCODE_CONFIG_CONTENT` pretty-printed; secrets masked) and the final
command are shown too. For a kimi provider run from a project root:

```text
provider   Kimi
harness    kimi
source     yaml
env file   ~/.config/agents/.env

project scope · ~/src/foo
  AGENTS.md         read in place (read natively — not injected)
  .agents/skills/   → .kimi-code/skills/

global scope
  ~/.config/agents/AGENTS.md   → ~/.kimi-code/AGENTS.md
  ~/.config/agents/skills/     → ~/.kimi-code/skills/

command
  kimi
```

For an **opencode** provider the resolved config is shown as indented JSON (the
`environment` section), and a scope with no sources is stated explicitly, e.g.:

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
  ~/.config/agents/skills/     → ~/.config/opencode/skills/
```

This is the same view [wrapper mode](wrapper.md#dry-run) shows — how
agedum renders the agent-neutral [source](source-shape.md) for the harness — plus the
resolved provider environment. Nothing is written to your real tree: the listed
destinations exist only inside the launched process's [mount namespace](internals.md).
