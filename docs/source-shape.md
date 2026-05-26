---
title: Source shape · agedum
description: The agent-neutral source agedum reads — a root AGENTS.md for instructions and .agents/skills/<name>/ for skills, with per-harness SKILL.<harness>.md overlays.
---

# Source shape

agedum reads a single **agent-neutral source** and renders it per harness at launch.
The source is the [`AGENTS.md`](https://agents.md) convention plus a sibling skills
tree:

```
my-project/
├── AGENTS.md                     # instructions (plain markdown)
└── .agents/
    └── skills/
        ├── review/
        │   ├── SKILL.md          # base skill: name + description + body
        │   ├── SKILL.claude.md   # optional Claude-only overlay
        │   ├── SKILL.kimi.md     # optional kimi-only overlay
        │   ├── checklist.md      # task file, copied verbatim
        │   └── lint.sh           # script, copied verbatim
        └── release/
            └── SKILL.md
```

Nothing here is harness-specific except the optional `SKILL.<harness>.md` overlays —
that is the whole point. You author once; agedum translates.

## AGENTS.md

A plain markdown file at the source root holding the standing instructions for the
agent — house style, conventions, what to do and not do. agedum copies it through to
each harness's instruction location unchanged:

- **Claude** → `CLAUDE.md` (its content is **not** rewritten — only relocated).
- **kimi** → folded into a generated `--agent-file` as the agent's additional role
  prompt.

See [Harnesses](harnesses.md) for exactly where it lands per harness, and
[Scopes](scopes.md) for the project vs global copies.

There is no front-matter contract on `AGENTS.md` — it is opaque markdown. Keep config
out of it; agedum carries instructions, not settings.

## Skills

A **skill** is a directory under `.agents/skills/<name>/`. The directory name is the
skill name. Each skill is rendered into the harness's skills location as a directory
of the same name.

### `SKILL.md` — the base

Every skill has a base `SKILL.md`: YAML front-matter (at minimum `name` and
`description`) followed by the skill body. This is the harness-neutral definition and
is used as-is when there is no overlay for the active harness.

```markdown
---
name: review
description: Review a change for correctness and house style before committing.
---

Walk the diff hunk by hunk. Flag anything that …
```

### `SKILL.<harness>.md` — per-harness overlay

When a skill needs something only one harness understands — e.g. Claude's
`allowed-tools` front-matter key, or harness-specific wording — put it in a
`SKILL.<harness>.md` file beside the base:

- `SKILL.claude.md` is applied when compiling for `--claude`.
- `SKILL.kimi.md` is applied when compiling for `--kimi`.
- An overlay for a *different* harness is ignored (a `SKILL.kimi.md` is skipped when
  compiling for Claude, and vice-versa).

The overlay is **merged** onto the base, not substituted:

| Part | Merge rule |
|---|---|
| Front-matter | Union of both; **overlay keys win** on conflict |
| Body | Base body, then a blank line, then the overlay body (concatenated) |

So a base that declares `name` / `description` plus a `SKILL.claude.md` that adds
`allowed-tools: Read, Bash` and an extra paragraph yields one `SKILL.md` carrying all
three front-matter keys and both bodies. If a skill has no base body, the overlay body
stands alone.

### Task files, scripts, and other assets

Any other file or subdirectory inside a skill — checklists, prompt fragments, helper
scripts, data — is **copied verbatim** into the rendered skill directory. The only
files treated specially are `SKILL.md` (the base) and `SKILL.<harness>.md` (overlays);
every other `*.md` and every non-markdown file is carried through unchanged.

!!! note "What counts as an overlay"
    Only files matching `SKILL.<something>.md` are treated as overlays and filtered
    out of the copy. A `README.md` or `checklist.md` inside a skill is a normal asset
    and is copied. Name your overlays exactly `SKILL.<harness>.md`.

## Locating the source

agedum does not require you to point at the source — it discovers it:

- **Project root** is the nearest ancestor of the current directory (including it)
  that contains `AGENTS.md`, a `.agents/` directory, or `.git`. Within that root,
  `AGENTS.md` and `.agents/skills/` are picked up if present.
- **Global source** is `~/.config/agents/AGENTS.md` (honouring `$XDG_CONFIG_HOME`) for
  instructions and `~/.agents/skills/` for skills.

Either scope's `AGENTS.md` or `skills/` may be absent — agedum just renders whatever it
finds. If *nothing* is found in either scope, it warns and runs your command with no
injected context. The two scopes are kept distinct rather than merged; see
[Scopes](scopes.md).
