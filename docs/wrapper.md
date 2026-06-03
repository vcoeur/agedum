---
title: Wrapper mode · agedum
description: Wrapper mode (agedum --wrapper <harness> -- <command>) compiles the agent-neutral source into a harness's native layout and runs your command inside a private mount namespace where the compiled files appear at their expected paths.
---

# Wrapper mode

```text
agedum --wrapper <harness> [--dry-run] -- <command> [args...]
```

Wrapper mode runs **any command** inside a private mount namespace where the
agent-neutral [source](source-shape.md) has been compiled to the chosen harness's native
layout and injected at the paths that harness already reads. It is the low-level entry
that [provider mode](provider.md) (`agedum <name>`, the normal way to launch) builds on —
provider mode is wrapper mode plus a resolved provider environment.

Reach for `--wrapper` directly only to front a harness with the injected context but **no
provider env** — e.g. native Claude with your own login — or to inspect what gets injected
with `--dry-run`.

```bash
agedum --wrapper claude   -- claude -p "…"     # render for Claude, run claude
agedum --wrapper kimi     -- kimi -p "…"       # render for kimi, run kimi
agedum --wrapper opencode -- opencode run "…"  # render for opencode, run opencode
agedum --wrapper cline    -- cline task "…"    # render for Cline, run cline
```

## Context and command are decoupled

The invocation has two halves split by a literal `--`:

- **before `--`** — `--wrapper <harness>` picks the **format** to compile to
  (`claude` / `kimi` / `opencode` / `cline`), plus the optional `--dry-run`.
- **after `--`** — the **command**, run verbatim, including its own binary and flags.
  agedum does not parse or rewrite it (a harness may get extra flags *appended* — see its
  page). `--wrapper=claude` is also accepted; an unknown harness or option is an error.

So the flag chooses what context the process sees, and everything after `--` is yours.

## How a harness resolves the source

Each `--wrapper <harness>` selects a **compiler**. A compiler renders the project and
global [scopes](scopes.md) into the harness's native layout and produces a *plan*: a set
of `(compiled-file → mount-target)` binds, plus any extra arguments to append to your
command. The [launcher](internals.md) injects the binds into the namespace and runs the
command. Every harness disposes of each source in one of three ways:

| Disposition | Meaning |
|---|---|
| **injected** (`→ <dest>`) | agedum writes a compiled copy and binds it at the path the harness reads (e.g. Claude's `CLAUDE.md`, every harness's skills dir). |
| **read in place** | the harness reads the file natively at its source location, so agedum injects nothing — and *cannot*, since the root `AGENTS.md` is git-tracked. kimi, opencode, and cline all read the **project** `AGENTS.md` this way. |
| **appended flag** | the context is passed as an extra argument, not a bind — only kimi's `--agent-file` for global instructions. |

Two rules hold for every harness:

- **Each scope lands at its own native location** — the project and global sources are
  never concatenated into one file. The harness reads both and applies its own precedence,
  exactly as if you had authored them by hand. See [Scopes](scopes.md).
- **Per-harness overlays are applied** — for skills, the base `SKILL.md` is merged with an
  optional [`SKILL.<harness>.md`](source-shape.md#skillharnessmd-per-harness-overlay); for
  the **global** `AGENTS.md`, an optional sibling
  [`AGENTS.<harness>.md`](source-shape.md#agentsharnessmd-per-harness-overlay-user-scope)
  is merged on. An overlay for a different harness is skipped.

The exact binds, targets, and any appended flags differ per harness — that is what each
harness page documents:

| Harness | Wrapper resolution | Provider config |
|---|---|---|
| Claude | [details](harnesses/claude.md#wrapper-resolution) | [details](harnesses/claude.md#provider-config) |
| kimi | [details](harnesses/kimi.md#wrapper-resolution) | [details](harnesses/kimi.md#provider-config) |
| opencode | [details](harnesses/opencode.md#wrapper-resolution) | [details](harnesses/opencode.md#provider-config) |
| Cline | [details](harnesses/cline.md#wrapper-resolution) | [details](harnesses/cline.md#provider-config) |

## Inspect without running — `--dry-run` { #dry-run }

Put `--dry-run` before `--` to compile the source and print the plan — the bind targets
and any appended args — without launching:

```bash
agedum --wrapper claude --dry-run -- claude   # list the injected virtual files, don't run
```

The output groups the dispositions by scope (project / global); the same view
[provider mode](provider.md#dry-run) shows, minus the resolved environment.

## No footprint

Nothing is written to your real tree or `$HOME`: the compiled files live only inside the
launched process's [mount namespace](internals.md), and agedum refuses to overlay a
git-tracked path. Wrapper mode is **Linux-only** and needs `bwrap`
([bubblewrap](https://github.com/containers/bubblewrap)) on `PATH`.

## Behaviour when no source is found

If neither the project nor the global [scope](scopes.md) has any `AGENTS.md` or skills,
agedum prints a warning to stderr and still runs your command — just with nothing
injected. It never blocks the launch on an empty source.
