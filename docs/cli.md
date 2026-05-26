---
title: CLI reference · agedum
description: The agedum invocation contract — context flags before the -- separator, the command after it, and the --version / --help options.
---

# CLI reference

```text
agedum (--claude | --kimi | --opencode) -- <command> [args...]
```

The invocation has two halves split by a literal `--`:

- **Before `--`** — one *context flag* selecting the harness format.
- **After `--`** — the command to run, **verbatim**, including its own binary and
  flags. agedum does not parse or rewrite it (some harnesses get extra flags
  *appended* — see [Harnesses](harnesses.md)).

Decoupling the context from the command keeps the flag space open for additional
`--<harness>` modes without touching how commands are passed.

## Context flags

| Flag | Effect |
|---|---|
| `--claude` | Render the source in Claude format ([details](harnesses.md#claude)). |
| `--kimi` | Render the source in kimi-cli format ([details](harnesses.md#kimi)). |
| `--opencode` | Render the source in opencode format ([details](harnesses.md#opencode)). |

Exactly one context mode is required. If several context flags are given, the last one
wins; an unknown option is an error.

## Other options

| Flag | Effect |
|---|---|
| `--version`, `-V` | Print `agedum <version>` and exit. |
| `-h`, `--help` | Print usage and exit. |

These are recognised only as the first argument, before any `--`.

## Examples

```bash
agedum --version
agedum --help

# Claude — interactive and headless
agedum --claude -- claude
agedum --claude -- claude --model sonnet -p "review this change"

# kimi
agedum --kimi -- kimi -p "explain this code"

# opencode
agedum --opencode -- opencode run "explain this code"
```

## Exit codes

agedum is transparent to your command's exit status: when the launch succeeds, agedum
**returns the child command's own exit code**. agedum-level failures use distinct
codes:

| Code | Meaning |
|---|---|
| *(child)* | The command ran; agedum propagates its exit code. |
| `1` | A launch error — e.g. `bwrap` not found, or a [git-tracked target](internals.md#safety) refused. |
| `2` | A usage error — missing `--`, no command, unknown flag, or no context mode. |

## Behaviour when no source is found

If neither the project nor the global [scope](scopes.md) has any `AGENTS.md` or skills,
agedum prints a warning to stderr and still runs your command — just with nothing
injected. It never blocks the launch on an empty source.
