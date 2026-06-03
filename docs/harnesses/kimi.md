---
title: kimi harness · agedum
description: How agedum drives kimi-cli — wrapper-mode resolution (project AGENTS.md read natively, global AGENTS.md via a generated --agent-file, skills binds) and the provider config that becomes appended CLI flags.
---

# kimi

kimi-cli reads the **project** `AGENTS.md` from the filesystem natively, but has **no
user-scope `AGENTS.md`** — so agedum leaves the project instructions in place and injects
only the global ones, via a flag. Skills, both scopes, are injected as binds like
[Claude's](claude.md).

## Wrapper resolution { #wrapper-resolution }

**Project instructions** — kimi merges every `AGENTS.md` from the project root (the
nearest `.git`) down to the working directory into its system prompt. The agent-neutral
source's `AGENTS.md` already sits at the project root, which is exactly where kimi looks,
so **agedum injects nothing** for it — and never tries to, since that root `AGENTS.md` is
typically git-tracked.

**Global instructions** — because kimi has no user-scope `AGENTS.md`, the global
`AGENTS.md` (base merged with an optional `AGENTS.kimi.md`
[overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope)) is injected
via a custom agent-file appended to your command:

```text
… kimi -p "…"  --agent-file /tmp/agedum-kimi-XXXX/agent.yaml
```

The generated `agent.yaml` extends kimi's default agent and injects the **global**
instructions as `system_prompt_args.ROLE_ADDITIONAL`:

```yaml
version: 1
agent:
  extend: default
  system_prompt_args:
    ROLE_ADDITIONAL: |
      <global AGENTS.md>
```

This coexists with native discovery: the default agent's system prompt fills
`ROLE_ADDITIONAL` from the agent-file (global) and a separate slot from the merged project
`AGENTS.md` (native) — so both scopes apply, each by the right mechanism.

**Skills** — bound into the directories kimi reads automatically:

| Source | Injected at |
|---|---|
| global `~/.agents/skills/` | `~/.kimi/skills/` |
| project `.agents/skills/` | `<root>/.kimi/skills/` |

- Skills use the `SKILL.kimi.md` overlay where present; assets are copied verbatim.
- The project-local `./.kimi/skills/` bind matches the layout kimi already auto-reads, so
  there is **no config rewrite** — `extra_args` carries only `--agent-file` (and only when
  a global `AGENTS.md` exists), never a `--config` override.
- A project with no global scope needs no `--agent-file` at all: its `AGENTS.md` is read
  natively. This mirrors the Claude harness — each scope kept distinct, never merged.

```bash
agedum --wrapper kimi -- kimi -p "explain this code"
```

## Provider config { #provider-config }

kimi reads its API token **from the environment**, so the key goes in `requiredEnv` (or
`secretEnv`, which is appended automatically) and is exported into the child unchanged. The
`config` knobs become **appended CLI flags** on the launched command:

```json
{
  "harness": "kimi",
  "slug": "kimi",
  "secretEnv": "MOONSHOT_API_KEY",
  "requiredEnv": ["MOONSHOT_API_KEY"],
  "config": { "model": "kimi-k2", "thinking": true }
}
```

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `thinking` | `--thinking` (true) / `--no-thinking` (false) |
| `plan` | `--plan` (true) |
| `configInline` | `--config <value>` |

The config above launches `kimi --model kimi-k2 --thinking`, with `MOONSHOT_API_KEY` in the
environment. `--prompt`/`--run` add `--prompt "<text>"` (and `--print` for `--run`) — see
the [prompt-seeding table](../provider.md#prompt-seeding).
