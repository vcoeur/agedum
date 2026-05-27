---
title: CLI reference · agedum
description: The agedum invocation contract — the wrapper mode (context flags before the -- separator) and the build-script mode (compile a provider config JSON into a shell wrapper), plus the --version / --help options.
---

# CLI reference

agedum has two modes.

```text
agedum --wrapper <claude|kimi|opencode> -- <command> [args...]   # run time
agedum --build-script [--check] <config.json> [output.sh]        # compile time
```

## Wrapper mode

```text
agedum --wrapper <harness> -- <command> [args...]
```

The invocation has two halves split by a literal `--`:

- **Before `--`** — `--wrapper <harness>` selects the harness format
  (`claude` / `kimi` / `opencode`).
- **After `--`** — the command to run, **verbatim**, including its own binary and
  flags. agedum does not parse or rewrite it (some harnesses get extra flags
  *appended* — see [Harnesses](harnesses.md)).

Decoupling the context from the command keeps the flag space open for additional
modes without touching how commands are passed.

| Flag | Effect |
|---|---|
| `--wrapper claude` | Render the source in Claude format ([details](harnesses.md#claude)). |
| `--wrapper kimi` | Render the source in kimi-cli format ([details](harnesses.md#kimi)). |
| `--wrapper opencode` | Render the source in opencode format ([details](harnesses.md#opencode)). |
| `--claude` / `--kimi` / `--opencode` | **Deprecated** aliases for `--wrapper <harness>`; print a notice to stderr and still run. |

`--wrapper claude` and `--wrapper=claude` are both accepted. A harness is required; an
unknown harness or option is an error.

```bash
agedum --wrapper claude -- claude
agedum --wrapper claude -- claude --model sonnet -p "review this change"
agedum --wrapper kimi -- kimi -p "explain this code"
agedum --wrapper opencode -- opencode run "explain this code"
```

## Build-script mode

```text
agedum --build-script [--check] <config.json> [output.sh]
```

Compile a condash-style **provider config JSON** into a standalone shell wrapper that
sets the provider/model/auth environment, then `exec`s `agedum --wrapper`. The harness
is read from the config's `harness` field. Full reference: [Build-script](build-script.md).

| Form | Effect |
|---|---|
| `agedum --build-script conf.json` | Write the generated wrapper to **stdout**. |
| `agedum --build-script conf.json out.sh` | Write the wrapper to `out.sh` (mode `0755`). |
| `agedum --build-script --check conf.json out.sh` | Exit non-zero if `out.sh` differs from a fresh generation (drift guard for CI). |

Unlike wrapper mode, build-script does **not** need `bwrap` — it only reads a JSON file
and emits text.

```bash
# Generate a provider wrapper:
agedum --build-script providers/claude-deepseek-auto.json bin/claude-deepseek-auto.sh

# Verify a committed wrapper is up to date (CI):
agedum --build-script --check providers/claude-deepseek-auto.json bin/claude-deepseek-auto.sh
```

## Other options

| Flag | Effect |
|---|---|
| `--version`, `-V` | Print `agedum <version>` and exit. |
| `-h`, `--help` | Print usage and exit. |

These are recognised only as the first argument.

## Exit codes

In wrapper mode agedum is transparent to your command's exit status: when the launch
succeeds, agedum **returns the child command's own exit code**. agedum-level failures
use distinct codes:

| Code | Meaning |
|---|---|
| *(child)* | Wrapper mode: the command ran; agedum propagates its exit code. |
| `0` | Build-script mode: the wrapper was written (or `--check` found it current). |
| `1` | A launch error (`bwrap` missing, a [git-tracked target](internals.md#safety)), a config error, or a `--check` mismatch. |
| `2` | A usage error — missing `--`, no command, unknown flag/harness, or a missing config path. |

## Behaviour when no source is found

In wrapper mode, if neither the project nor the global [scope](scopes.md) has any
`AGENTS.md` or skills, agedum prints a warning to stderr and still runs your command —
just with nothing injected. It never blocks the launch on an empty source.
