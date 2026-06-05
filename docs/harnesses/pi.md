---
title: pi harness · agedum
description: How agedum drives pi (earendil-works pi-coding-agent) — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md to ~/.pi/agent/AGENTS.md, skills to .pi/skills) and the provider config mapped to pi's CLI flags, with a generated models.json for custom endpoints and settings.json for pi-subagents model routing.
---

# pi

[pi](https://pi.dev) (the earendil-works / `@mariozechner` coding agent,
`@earendil-works/pi-coding-agent`) is a minimal terminal coding agent. Like
[opencode](opencode.md), [Cline](cline.md), and [reasonix](reasonix.md), it is **pure
path-discovery** in wrapper mode — it reads `AGENTS.md`/`CLAUDE.md` and skills from fixed
locations and needs no flags. So the project instructions stay in place and everything else
is a bind.

## Wrapper resolution { #wrapper-resolution }

| Source | Injected at |
|---|---|
| project `AGENTS.md` | *(not injected — read natively, cwd→root walk)* |
| project `.agents/skills/` | `<root>/.pi/skills/` |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.pi.md` overlay) | `~/.pi/agent/AGENTS.md` |
| global `~/.config/agents/skills/` | `~/.pi/agent/skills/` |

- **Project instructions** — pi walks from the cwd up to the filesystem root collecting
  `AGENTS.md`/`CLAUDE.md` (`loadProjectContextFiles`), so the project-root `AGENTS.md` — the
  agent-neutral source, already in place — is read **natively**. agedum injects nothing for
  it (and never could, since the root `AGENTS.md` is git-tracked). `pi --no-context-files`
  disables this discovery entirely.
- **Global instructions** — pi reads a user-global context file from its agent dir
  (`~/.pi/agent`, `getAgentDir()`), with `AGENTS.md` the first candidate. So the global
  `AGENTS.md` is bound to `~/.pi/agent/AGENTS.md` — base merged with an optional `AGENTS.pi.md`
  [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope). The agent dir
  honours `$PI_CODING_AGENT_DIR`.
- **Skills** — pi auto-discovers `SKILL.md` folders from `./.pi/skills/` (project) and
  `~/.pi/agent/skills/` (global). Each skill carries `name`/`description` frontmatter — the
  neutral source shape — compiled with the `SKILL.pi.md` overlay and bound to those two dirs.
- `extra_args`: **none** — pi discovers everything from disk, like Claude, opencode, Cline,
  and reasonix.

```bash
agedum --wrapper pi -- pi               # drive pi with the same source
agedum --wrapper pi --dry-run -- pi     # show what would be injected
```

## Provider config { #provider-config }

pi's model/provider/thinking are plain CLI flags, and its API key rides the environment under
its conventional name. A custom endpoint and pi-subagents model routing have no flags, so they
become **generated on-disk config files** (the [reasonix.toml](reasonix.md#custom-endpoint)
precedent) — merged onto your existing files so they augment rather than mask them.

### Built-in provider (no `baseUrl`)

```json
{
  "harness": "pi",
  "slug": "pi-sonnet",
  "secretEnv": "ANTHROPIC_API_KEY",
  "config": {
    "model": "anthropic/claude-sonnet-4",
    "thinking": "high"
  }
}
```

| `config` key | Effect |
|---|---|
| `model` | `--model <pattern>` — pi's `provider/id[:thinking]` form |
| `provider` | `--provider <name>` (pi's default is `google`) |
| `thinking` | `--thinking <level>` — `off` \| `minimal` \| `low` \| `medium` \| `high` \| `xhigh` |
| `secretEnv` value | exported into the child (`requiredEnv`); pi reads it by name (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `GOOGLE_API_KEY` / …) |

This launches `pi --model anthropic/claude-sonnet-4 --thinking high`. **No key flag is
appended and no secret lands in argv** — agedum exports `secretEnv` under its own name via the
normal [required-env](../provider.md#the-env-file) path, the same as [aider](aider.md). The
token is masked in `--dry-run`.

### Custom endpoint (`baseUrl`) { #custom-endpoint }

pi has no `--base-url` flag — a custom OpenAI-/Anthropic-compatible endpoint is a provider
block in `~/.pi/agent/models.json`. So when a config sets `baseUrl`, agedum **generates that
file** (merged onto any existing `models.json`) defining a provider named `agedum`, and selects
it with `--model agedum/<id>`:

```json
{
  "harness": "pi",
  "slug": "pi-deepseek",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "baseUrl": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api": "openai-completions"
  }
}
```

| `config` key | Effect |
|---|---|
| `baseUrl` | the endpoint → the provider's `baseUrl`; its presence switches on generation |
| `model` | the **upstream model id** at that endpoint (here *not* a `provider/id` pattern). Required when `baseUrl` is set; selected as `--model agedum/<model>` |
| `api` | pi's provider API (default `openai-completions`; also `anthropic-messages`, `openai-responses`, `google-generative-ai`, …) |
| `models` | optional extra upstream model ids to register at the endpoint |
| `secretEnv` | referenced in `apiKey` as `$VAR` (omitted for a keyless endpoint) |

The generated file (shown by `--dry-run`) is:

```json
{
  "providers": {
    "agedum": {
      "baseUrl": "https://api.deepseek.com/v1",
      "api": "openai-completions",
      "apiKey": "$DEEPSEEK_API_KEY",
      "models": [{ "id": "deepseek-chat" }]
    }
  }
}
```

**The key never lands on disk** — `apiKey` references it by env-var *name* (`$DEEPSEEK_API_KEY`),
and pi resolves the value from the exported environment at runtime. The file is **merged** onto
any existing `~/.pi/agent/models.json` (deep-merge, agedum keys win), so your other custom
providers survive the session. The bind goes through the normal launcher path, so it is refused
over a git-tracked target.

### Subagent model routing (`subagentModel`) { #subagent-routing }

With the [pi-subagents](https://pi.dev/packages/pi-subagents) extension, each built-in
subagent's model is set in pi's `settings.json` under `subagents.agentOverrides`. So
`subagentModel` makes agedum **generate `~/.pi/agent/settings.json`** routing every built-in
agent — `scout`, `researcher`, `planner`, `worker`, `reviewer`, `context-builder`, `oracle`,
`delegate` — to one model (the [opencode](opencode.md)-flash / reasonix-`subagentModel`
analog). It is merged onto your existing `settings.json`, so other settings are untouched.

A strong executor with a fast subagent tier on one custom endpoint — both ids land in the
generated `models.json` `models` list, and the subagent override routes to `agedum/<sub>`:

```json
{
  "harness": "pi",
  "slug": "pi-deepseek-flash",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "baseUrl": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "subagentModel": "deepseek-flash"
  }
}
```

→ launches `pi --model agedum/deepseek-chat`, with `models.json` registering `deepseek-chat`
+ `deepseek-flash` on the `agedum` provider and `settings.json` routing every built-in subagent
to `agedum/deepseek-flash`. Without `baseUrl`, `model`/`subagentModel` are passed through as
pi's built-in `provider/id` patterns (e.g. `anthropic/claude-haiku-4-5`).

### Cross-provider multi-agent (`providerDef`) { #provider-def }

When the executor and the fast subagents live on **different** providers (e.g. a Kimi executor
with DeepSeek-flash subagents — the [reasonix](reasonix.md#two-model)-`kimi-flash` analog), the
single-`baseUrl` shorthand isn't enough: `models.json` needs more than one provider. Use
**`providerDef`** — a single object or a **list** of `{id, api, baseUrl, model, apiKeyEnv}`,
each rendered as a `models.json` provider block. `model` and `subagentModel` are then pi
`provider/id` patterns referencing those ids, passed through verbatim:

```json
{
  "harness": "pi",
  "slug": "pi-kimi-flash",
  "requiredEnv": ["KIMI_API_KEY", "DEEPSEEK_API_KEY"],
  "config": {
    "model": "kimi/k2p6",
    "subagentModel": "deepseek/deepseek-v4-flash",
    "providerDef": [
      { "id": "kimi", "api": "anthropic-messages", "baseUrl": "https://api.kimi.com/coding", "model": "k2p6", "apiKeyEnv": "KIMI_API_KEY" },
      { "id": "deepseek", "api": "openai-completions", "baseUrl": "https://api.deepseek.com", "model": "deepseek-v4-flash", "apiKeyEnv": "DEEPSEEK_API_KEY" }
    ]
  }
}
```

| `providerDef` field | Effect |
|---|---|
| `id` | the pi provider name; models are selected as `<id>/<model>` |
| `baseUrl` | the endpoint → the provider's `baseUrl` |
| `model` | the upstream model id served there → the provider's one `models[]` entry |
| `api` | the provider API (default `openai-completions`; `anthropic-messages` for a Kimi/Anthropic endpoint) |
| `apiKeyEnv` | referenced as `apiKey: "$VAR"` (never written); auto-added to `requiredEnv` and exported |

→ a `models.json` with a `kimi` and a `deepseek` provider, `pi --model kimi/k2p6`, and
`settings.json` routing every built-in subagent to `deepseek/deepseek-v4-flash`. `baseUrl` and
`providerDef` are **mutually exclusive** — `baseUrl` is the single-endpoint shorthand,
`providerDef` the explicit multi-provider form. (`api: anthropic-messages` POSTs to
`<baseUrl>/v1/messages`, so a coding endpoint mounted at `…/coding/v1/messages` uses
`baseUrl: "…/coding"`.)

**`--prompt`/`--run`.** pi takes the prompt as its positional argument either way; `--print`
(`-p`) flips it to non-interactive. So `--prompt` seeds the interactive TUI (`pi "<text>"`) and
`--run` runs once and exits (`pi --print "<text>"`) — the [Claude](claude.md) shape exactly.
See the [prompt-seeding table](../provider.md#prompt-seeding).
