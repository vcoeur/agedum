---
title: Harnesses · agedum
description: One page per agent CLI agedum drives — Claude, kimi, opencode, Cline, reasonix, and aider. Each documents how wrapper mode resolves the agent-neutral source for it and how to write a provider config for it.
---

# Harnesses

A **harness** is an agent CLI agedum drives. Each has its own page documenting two things:

- **Wrapper resolution** — where [wrapper mode](../wrapper.md) lands the agent-neutral
  [source](../source-shape.md) (which files are injected, which are read in place, any
  appended flags).
- **Provider config** — how to write a [provider](../provider.md) config for it (the
  `config` block mapping, with a working recipe).

| Harness | Reads project `AGENTS.md` | Global instructions land at | `extra_args` | Provider token |
|---|---|---|---|---|
| [Claude](claude.md) | injected → `CLAUDE.md` | `~/.claude/CLAUDE.md` | none | `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` env |
| [kimi](kimi.md) | read in place | a generated `--agent-file` | `--agent-file` | env (`requiredEnv` export) |
| [opencode](opencode.md) | read in place | `~/.config/opencode/AGENTS.md` | none | `OPENCODE_CONFIG_CONTENT` doc (or own auth) |
| [Cline](cline.md) | read in place | `~/.agents/AGENTS.md` | none | `--key` argv flag |
| [reasonix](reasonix.md) | read in place | `~/.config/reasonix/AGENTS.md` | none | env via `api_key_env` (`requiredEnv` export) |
| [aider](aider.md) | injected → `--read` | a second `--read` | `--read` (×N) | env (litellm, `requiredEnv` export) |

Skills are binds at the harness's own skills dir, compiled with the matching
`SKILL.<harness>.md` overlay — in every harness **except [aider](aider.md)**, which has no
skills mechanism (agedum injects only its `AGENTS.md`, via `--read`). The shared mechanics —
scopes, overlays, the namespace launch — live in [Wrapper mode](../wrapper.md) and
[Source & scopes](../source-shape.md).

## Adding a harness

A new harness is a single compiler function `compile_<harness>(project, global_, dest) ->
Plan` plus, for provider mode, an env/command builder. The launcher and safety rules are
shared, so it inherits the namespace, git-safety, and cleanup for free. See
[Internals](../internals.md#adding-a-harness).
