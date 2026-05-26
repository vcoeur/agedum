---
title: Harnesses · agedum
description: Exactly what agedum does for each harness command — the Claude (--claude) and kimi (--kimi) compilers, the targets they inject, and the extra flags they append.
---

# Harnesses

The flag before `--` selects a **compiler**. Each compiler renders the agent-neutral
[source](source-shape.md) into one harness's native layout and produces a *plan* — a
set of `(compiled-file → mount-target)` binds, plus any extra arguments to append to
your command. The [launcher](internals.md) then injects the binds into a private mount
namespace and runs the command. Context and command are decoupled: the flag picks the
format; everything after `--` is run verbatim.

```bash
agedum --claude   -- claude -p "…"       # render for Claude, run claude
agedum --kimi     -- kimi -p "…"         # render for kimi, run kimi
agedum --opencode -- opencode run "…"    # render for opencode, run opencode
```

Every compiler processes the project and global [scopes](scopes.md) and applies the
[skill overlay rules](source-shape.md#skillharnessmd-per-harness-overlay) for its
harness (`SKILL.claude.md` for Claude, `SKILL.kimi.md` for kimi, `SKILL.opencode.md` for
opencode; other harnesses' overlays are skipped).

## `--claude` { #claude }

Claude discovers its context purely from the filesystem, so agedum injects files at the
paths Claude already reads. Nothing is appended to your command.

| Source | Injected at |
|---|---|
| project `AGENTS.md` | `<root>/CLAUDE.md` |
| project `.agents/skills/` | `<root>/.claude/skills/` |
| global `~/.config/agents/AGENTS.md` | `$CLAUDE_CONFIG_DIR/CLAUDE.md` (default `~/.claude/CLAUDE.md`) |
| global `~/.agents/skills/` | `$CLAUDE_CONFIG_DIR/skills/` (default `~/.claude/skills/`) |

- Each scope lands at **its own** location — never concatenated. The project
  `CLAUDE.md` carries only project instructions; the user `CLAUDE.md` only the global
  ones. Claude reads both and applies its own precedence. See [Scopes](scopes.md).
- For each skill, the base `SKILL.md` is merged with an optional `SKILL.claude.md`
  overlay (front-matter union with overlay winning, bodies concatenated), then task
  files and scripts are copied verbatim.
- Only `~/.claude/CLAUDE.md` and `~/.claude/skills/` are overlaid in the user config
  dir — your `~/.claude.json` auth and other settings are untouched.
- `extra_args`: **none**. The command runs exactly as you wrote it.

```bash
# Interactive Claude with project + global context injected:
agedum --claude -- claude

# Headless review with a specific model:
agedum --claude -- claude --model sonnet -p "review this change"
```

## `--kimi` { #kimi }

kimi reads the **project** `AGENTS.md` from the filesystem natively, but has **no
user-scope `AGENTS.md`** — so agedum leaves the project instructions in place and
injects only the global ones, via a flag. Skills, both scopes, are injected as binds
like Claude's.

**Project instructions** — kimi merges every `AGENTS.md` from the project root (the
nearest `.git`) down to the working directory into its system prompt. The
agent-neutral source's `AGENTS.md` already sits at the project root, which is exactly
where kimi looks, so **agedum injects nothing** for it — and never tries to, since that
root `AGENTS.md` is typically git-tracked.

**Global instructions** — because kimi has no user-scope `AGENTS.md`, the global
`AGENTS.md` is injected via a custom agent-file appended to your command:

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
`ROLE_ADDITIONAL` from the agent-file (global) and a separate slot from the merged
project `AGENTS.md` (native) — so both scopes apply, each by the right mechanism.

**Skills** — bound into the directories kimi reads automatically:

| Source | Injected at |
|---|---|
| global `~/.agents/skills/` | `~/.kimi/skills/` |
| project `.agents/skills/` | `<root>/.kimi/skills/` |

- Skills use the `SKILL.kimi.md` overlay where present; assets are copied verbatim.
- The project-local `./.kimi/skills/` bind matches the layout kimi already auto-reads,
  so there is **no config rewrite** — `extra_args` carries only `--agent-file` (and only
  when a global `AGENTS.md` exists), never a `--config` override.
- A project with no global scope needs no `--agent-file` at all: its `AGENTS.md` is read
  natively. This mirrors the Claude harness — each scope kept distinct, never merged.

```bash
agedum --kimi -- kimi -p "explain this code"
```

## `--opencode` { #opencode }

opencode is **pure path-discovery** — it reads instructions and skills from fixed
locations and needs no flags — so every scope is a bind and nothing is appended to your
command.

| Source | Injected at |
|---|---|
| project `AGENTS.md` | *(not injected — read natively at `./AGENTS.md`)* |
| project `.agents/skills/` | `<root>/.opencode/skills/` |
| global `~/.config/agents/AGENTS.md` | `$XDG_CONFIG_HOME/opencode/AGENTS.md` (default `~/.config/opencode/AGENTS.md`) |
| global `~/.agents/skills/` | `$XDG_CONFIG_HOME/opencode/skills/` (default `~/.config/opencode/skills/`) |

- **Project instructions** — opencode reads the root `AGENTS.md` (traversing up from the
  work dir) as its project rules file. That is exactly the agent-neutral source, already
  in place, so **agedum injects nothing** for it — and never could, since the root
  `AGENTS.md` is git-tracked.
- **Global instructions** — opencode reads `~/.config/opencode/AGENTS.md` as its
  user-scope rules file, so the global `AGENTS.md` is bound there.
- **Skills** — compiled with the `SKILL.opencode.md` overlay and bound to
  `./.opencode/skills/` (project) and `~/.config/opencode/skills/` (global). opencode
  searches those directories **before** `.agents/skills/` / `~/.agents/skills/` (which it
  would otherwise read directly), so the overlaid copy wins over the raw source.
- `extra_args`: **none** — opencode discovers everything from disk, like Claude.

This is the closest harness to Claude: pure binds, each scope at its own native
location, never merged. The one difference is that the project instructions are read in
place rather than relocated.

```bash
agedum --opencode -- opencode run "review this change"
agedum --opencode -- opencode            # interactive TUI
```

## Other harnesses

`--<harness>-variant` composition is a planned follow-up. Adding a harness is a new
compiler function returning the same plan shape (binds + extra args); the launcher and
CLI are harness-agnostic. See [Internals](internals.md) for the plan/launch contract.
