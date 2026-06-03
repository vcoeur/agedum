---
title: CLI reference · agedum
description: The agedum invocation contract — the provider mode (launch a harness from a provider config JSON) and the wrapper mode (context flags before the -- separator), plus the --version / --help options.
---

# CLI reference

agedum has two modes. **Provider mode (`agedum <name>`) is the normal way to launch** —
it reads a provider config, sets the model/auth env, and injects the context. Wrapper
mode is the lower-level entry it builds on: run any command in the injected context with
no provider env.

```text
agedum <provider-name|config.json> [--env <file>] [--dry-run] [harness args...]
agedum --wrapper <claude|kimi|opencode|cline> [--dry-run] -- <command> [args...]
```

## Provider mode

```text
agedum <provider-name|config.json> [--env <file>] [--dry-run] [harness args...]
```

Launch a harness from a condash-style **provider config JSON**. agedum resolves the
provider's env from the env file, sets the provider/model/auth environment, and launches
the harness named in the config — inside the virtual-file context. The single positional
is a provider **name** (resolved under `$AGENTS_PROVIDERS_DIR`, default
`~/.config/agents/providers`) or a **path** (it contains `/` or ends in `.json`). Full
reference: [Provider mode](provider.md).

| Form | Effect |
|---|---|
| `agedum claude-deepseek-auto` | Resolve the named provider and launch its harness. |
| `agedum ./conf.json -p "hi"` | Launch from a config path; pass `-p "hi"` to the harness. |
| `agedum <provider> --env <file>` | Read secrets from `<file>` instead of the default env file. |
| `agedum <provider> --prompt "<text>"` | Seed the harness with an initial prompt, then stay interactive. |
| `agedum <provider> --run "<text>"` | Run the prompt non-interactively, then exit (no interactive UI). |
| `agedum <provider> --dry-run` | Print the resolved env (secrets masked), the injected virtual files, and the argv; don't launch. |

`--env` and `--dry-run` are agedum's own flags and are recognised **before or after** the
provider. Any other token after the provider is passed to the harness verbatim; reach for
a `--` to forward a literal `--dry-run`/`--env` to the harness (`agedum <provider> -- --dry-run`).

```bash
agedum claude-deepseek-auto
agedum claude-deepseek-auto -p "review this change"
agedum opencode-deepseek run "explain this code"
agedum claude-deepseek-auto --dry-run
```

### Seeding a prompt — `--prompt` and `--run`

Two agedum flags seed the harness with an initial prompt, so you don't have to know each
harness's own prompt syntax. They are mutually exclusive (each may be given once):

- **`--prompt "<text>"`** — start the harness **interactively**, with `<text>` as the first
  message; the session stays open for follow-ups.
- **`--run "<text>"`** — run `<text>` **non-interactively** and exit (no interactive UI).
  This is the form to use from scripts, tasks, and cron.

agedum translates the flag to each harness's native invocation (verify it with `--dry-run`):

| Harness | `--prompt "<text>"` (interactive) | `--run "<text>"` (non-interactive) |
|---|---|---|
| claude | `claude "<text>"` | `claude --print "<text>"` |
| kimi | `kimi --prompt "<text>"` | `kimi --prompt "<text>" --print` |
| opencode | `opencode --prompt "<text>"` | `opencode run "<text>"` |

If a harness has no known prompt-seeding convention, agedum **fails loudly** with a clear
error rather than launching the wrong way. Any harness passthrough args you add are kept,
placed before the prompt text.

A `--run` launch is non-interactive, so agedum gives the harness **`/dev/null` for stdin**:
the whole task is already in argv, and the harness must never block waiting on input. (This
matters for `opencode run`, which otherwise hangs forever on an open, non-tty stdin — e.g.
when launched from a pipe or a headless task runner.) `--prompt` and a bare launch keep the
inherited stdin so the live session can read your keystrokes.

```bash
agedum claude-deepseek-auto --prompt "review this change"   # interactive, seeded
agedum claude-deepseek-auto --run "summarise the diff"      # run once, then exit
agedum opencode-deepseek --run "explain this code"
```

## Wrapper mode

```text
agedum --wrapper <harness> [--dry-run] -- <command> [args...]
```

Most users never type this — [provider mode](#provider-mode) calls it internally. Reach
for it to front a harness with the injected context but no provider env (e.g. native
Claude with your own login), or to inspect what gets injected with `--dry-run`.

The invocation has two halves split by a literal `--`:

- **Before `--`** — `--wrapper <harness>` selects the harness format
  (`claude` / `kimi` / `opencode` / `cline`), plus the optional `--dry-run`.
- **After `--`** — the command to run, **verbatim**, including its own binary and
  flags. agedum does not parse or rewrite it (some harnesses get extra flags
  *appended* — see [Wrapper mode](wrapper.md)).

Decoupling the context from the command keeps the flag space open for additional
modes without touching how commands are passed.

| Flag | Effect |
|---|---|
| `--wrapper claude` | Render the source in Claude format ([details](harnesses/claude.md#wrapper-resolution)). |
| `--wrapper kimi` | Render the source in kimi-cli format ([details](harnesses/kimi.md#wrapper-resolution)). |
| `--wrapper opencode` | Render the source in opencode format ([details](harnesses/opencode.md#wrapper-resolution)). |
| `--wrapper cline` | Render the source in Cline format ([details](harnesses/cline.md#wrapper-resolution)). |
| `--dry-run` | Print the virtual files that would be injected (and any appended args), then exit without running the command. |

`--wrapper claude` and `--wrapper=claude` are both accepted. A harness is required; an
unknown harness or option is an error.

```bash
agedum --wrapper claude -- claude
agedum --wrapper claude -- claude --model sonnet -p "review this change"
agedum --wrapper kimi -- kimi -p "explain this code"
agedum --wrapper opencode --dry-run -- opencode   # show what would be injected
agedum --wrapper cline -- cline task "review this change"
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
| `0` | A `--dry-run` (either mode): the resolved env / virtual files / argv were printed. |
| `1` | A launch error (`bwrap` missing, a [git-tracked target](internals.md#safety)), or a provider-config error (unreadable/invalid JSON, unknown harness, a missing required env var). |
| `2` | A usage error — missing `--`, no command, unknown flag/harness, or a missing provider. |

## Behaviour when no source is found

In wrapper mode, if neither the project nor the global [scope](scopes.md) has any
`AGENTS.md` or skills, agedum prints a warning to stderr and still runs your command —
just with nothing injected. It never blocks the launch on an empty source.
