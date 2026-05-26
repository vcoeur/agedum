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
agedum --claude -- claude -p "…"     # render for Claude, run claude
agedum --kimi   -- kimi -p "…"       # render for kimi, run kimi
```

Both compilers process the project and global [scopes](scopes.md) and apply the
[skill overlay rules](source-shape.md#skillharnessmd-per-harness-overlay) for their
harness (`SKILL.claude.md` for Claude, `SKILL.kimi.md` for kimi; other harnesses'
overlays are skipped).

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

kimi does **not** discover instructions from a fixed file path — they are supplied via
a flag — so agedum *augments* the command in addition to binding files. Skills, by
contrast, kimi reads from directories, so those are injected as binds like Claude's.

**Instructions** — the global and project `AGENTS.md` (in that order) are merged into a
single transient agent-file and appended to your command:

```text
… kimi -p "…"  --agent-file /tmp/agedum-kimi-XXXX/agent.yaml
```

The generated `agent.yaml` extends kimi's default agent and injects the merged
instructions as `system_prompt_args.ROLE_ADDITIONAL`:

```yaml
version: 1
agent:
  extend: default
  system_prompt_args:
    ROLE_ADDITIONAL: |
      <global AGENTS.md>

      <project AGENTS.md>
```

**Skills** — bound into the directories kimi reads automatically:

| Source | Injected at |
|---|---|
| global `~/.agents/skills/` | `~/.kimi/skills/` |
| project `.agents/skills/` | `<root>/.kimi/skills/` |

- Skills use the `SKILL.kimi.md` overlay where present; assets are copied verbatim.
- The project-local `./.kimi/skills/` bind matches the layout kimi already auto-reads,
  so there is **no config rewrite** — `extra_args` carries only `--agent-file`, never a
  `--config` override.
- Unlike Claude, kimi's instructions *are* merged across scopes (one `--agent-file`
  holding global then project), because kimi takes a single additional-role prompt.
  Skills stay split across the two directories.

```bash
agedum --kimi -- kimi -p "explain this code"
```

## Other harnesses

`opencode` and `--<harness>-variant` composition are planned. Adding a harness is a new
compiler function returning the same plan shape (binds + extra args); the launcher and
CLI are harness-agnostic. See [Internals](internals.md) for the plan/launch contract.
