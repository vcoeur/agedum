---
title: Provider mode · agedum
description: Launch a harness from a provider config JSON — agedum resolves the provider's env from a .env, validates the required vars, sets the provider/model/auth environment, and launches the harness inside the virtual-file context.
---

# Provider mode

```text
agedum <provider-name|config.json> [--env <file>] [--dry-run] [harness args...]
```

Provider mode is the **normal way to launch** an agent. agedum reads a **provider config
JSON**, resolves the provider's secrets from a `.env`, sets the provider/model/auth
environment, and launches the harness named in the config — all in one process, inside the
same virtual-file context [wrapper mode](wrapper.md) uses. There is no generated launcher
script: the config JSON is read at run time.

```bash
agedum claude-deepseek-auto                   # resolve the named provider, launch claude
agedum claude-deepseek-auto -p "review this"  # extra args go to the harness
agedum ./providers/my-claude.json             # a path instead of a name
agedum claude-deepseek-auto --dry-run         # print the resolved env + argv, don't launch
```

This page covers the mechanism shared by every provider: how the config and env resolve,
the config envelope, prompt-seeding, and `--dry-run`. The **`config` block is per-harness**
— each harness page has a working recipe and the full key mapping:

| Harness | Provider config |
|---|---|
| Claude | [recipe + mapping](harnesses/claude.md#provider-config) |
| kimi | [recipe + mapping](harnesses/kimi.md#provider-config) |
| opencode | [recipe + mapping](harnesses/opencode.md#provider-config) |
| Cline | [recipe + mapping](harnesses/cline.md#provider-config) |
| reasonix | [recipe + mapping](harnesses/reasonix.md#provider-config) |
| aider | [recipe + mapping](harnesses/aider.md#provider-config) |
| pi | [recipe + mapping](harnesses/pi.md#provider-config) |

## Resolving the provider

The single positional argument is **a name or a path**:

- **path** — it contains `/` or ends in `.json`. Absolute as-is, otherwise relative to the
  current directory.
- **name** — anything else. Resolved to
  `${AGENTS_PROVIDERS_DIR:-~/.config/agents/providers}/<name>.json`. Run
  [`agedum --providers`](cli.md#listing-providers) to list the names available there.

Any token after the provider that isn't an agedum flag is passed to the harness verbatim
(`agedum claude-deepseek-auto -p "hi"` runs `claude -p "hi"`). `--env` and `--dry-run` are
agedum's own flags and may appear **before or after** the provider; to forward a literal
`--dry-run`/`--env` to the harness, put it after a `--`
(`agedum claude-deepseek-auto -- --dry-run`).

## The env file

Secrets are read from `${AGENTS_ENV_FILE:-~/.config/agents/.env}`, overridable per-run with
`--env <file>`. It is a simple `KEY=VALUE` file (an optional `export ` prefix and
surrounding quotes are honoured; `#` lines and blanks are skipped). Every variable named in
the config's `requiredEnv` (plus `secretEnv`) must be present and non-empty, or agedum
fails fast with a clear message before launching.

Unlike the retired `--build-script` codegen — which emitted a wrapper that sourced the
`.env` itself, so agedum never saw a token — provider mode reads the env file into the
agedum process and sets the resolved values in the child environment.

## Config shape

The config is the condash-style agent envelope:

```json
{
  "harness": "claude",
  "name": "Claude Deepseek Auto",
  "slug": "claude-deepseek-auto",
  "secretEnv": "DEEPSEEK_API_KEY",
  "requiredEnv": ["DEEPSEEK_API_KEY"],
  "config": { "...": "per-harness options" }
}
```

| Field | Meaning |
|---|---|
| `harness` | `claude`, `kimi`, `opencode`, `cline`, `reasonix`, `aider`, or `pi`. Selects the translation **and** the harness to launch; read from the file (there is no `--harness` flag). |
| `secretEnv` | The env var holding the API token. Per harness: `claude` maps it to `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`; `kimi` / `opencode` / `reasonix` / `aider` / `pi` pass it through under its own name (reasonix reads it via the provider's `api_key_env`, aider via litellm, pi by the conventional var name or a `$VAR` reference in a generated `models.json`); `cline` passes it as `--key`. |
| `requiredEnv` | Vars validated and exported into the child. `secretEnv` is always appended if not listed. Declare a provider's API-key var here so a harness that reads it from the environment sees it. |
| `config` | The per-harness option block — see the harness page table above. |
| `sandbox` | Optional **write-confinement** — mount the host read-only and let the harness write only to the project root, agedum's injection dirs, `/tmp`, and the paths in `sandbox.readWrite`. See [Filesystem sandbox](#sandbox). |
| `name` / `slug` | Labels; `slug` (else `name`, else the harness) names the provider in error and `--dry-run` messages. |

Save the file as `<slug>.json` under `~/.config/agents/providers/` (or anywhere, and launch
it by path), put the API token in `~/.config/agents/.env`, then `agedum <slug> --dry-run` to
check it before launching for real.

## Filesystem sandbox — `sandbox` { #sandbox }

An optional top-level `sandbox` block confines what the launched harness can **write**.
Without it, the harness shares your whole filesystem read-write — the namespace isolates only
*what the harness reads as config*, not where it can write. With it, the host is mounted
**read-only** and the harness can write only to the **project root**, the dirs agedum injects
into so the harness keeps its own state (e.g. `~/.claude`), a private `/tmp`, and each path in
**`readWrite`**:

```json
{
  "harness": "claude",
  "slug": "claude-boxed",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": { "...": "..." },
  "sandbox": {
    "readWrite": ["~/notes", "${PROJECT_ROOT}/build"]
  }
}
```

`readWrite` paths are templates resolved at launch: `~` → your home, `$VAR` → the
environment, `${PROJECT_ROOT}` → the project root. A path already inside the project root is
redundant (the project root is always writable) and folded in. An empty `"sandbox": {}` still
confines — only the always-writable set applies. This is the provider-mode equivalent of
wrapper mode's [`--sandbox` / `--rw-dir`](wrapper.md#sandbox); `agedum <name> --dry-run` lists
the resulting writable set under a `sandbox · write-confinement` heading. It confines the
**filesystem** only — the network is untouched, so the harness still reaches its endpoint.
Linux-only, like the rest of the launch.

## Seeding an initial prompt — `--prompt` / `--run` { #prompt-seeding }

Two agedum flags seed the launched harness with a first prompt, abstracting over each
harness's own prompt syntax (mutually exclusive, each given once):

- **`--prompt "<text>"`** — launch **interactively** with `<text>` as the first message;
  the session stays open.
- **`--run "<text>"`** — run `<text>` **non-interactively** and exit. The form for scripts
  and tasks.

agedum maps the flag to the harness named in the config:

| Harness | `--prompt` (interactive) | `--run` (non-interactive) |
|---|---|---|
| claude | positional prompt: `claude "<text>"` | `claude --print "<text>"` |
| kimi | `kimi --prompt "<text>"` | `kimi --prompt "<text>" --print` |
| opencode | `opencode --prompt "<text>"` | `opencode run "<text>"` |
| cline | `cline --tui "<text>"` | `cline "<text>"` |
| reasonix | *(unsupported — fail-loud)* | `reasonix run "<text>"` |
| aider | *(unsupported — fail-loud)* | `aider --message "<text>"` |
| pi | positional prompt: `pi "<text>"` | `pi --print "<text>"` |

For cline the prompt is a positional argument either way; `--tui` is what opens the
interactive TUI (seeded with the prompt), and a bare positional runs the task once in act
mode and exits. For reasonix and aider only `--run` is supported — reasonix's `run`
subcommand and aider's `--message` each take the task and exit, but neither has an
interactive prompt-seed (reasonix's `chat` can't be pre-seeded; aider's `--message` exits),
so `--prompt` is a fail-loud `ProviderError` (condash then falls back to spawn-and-type for
an interactive seed).

A harness with no known prompt-seeding convention is a fail-loud `ProviderError` (agedum
never guesses). Harness passthrough args are preserved, before the prompt text.

Because `--run` is non-interactive, agedum runs the harness with **`/dev/null` for stdin**
so it can never block on input it will never receive — notably `opencode run`, which hangs
forever on an open, non-tty stdin (a pipe, a headless task runner). `--prompt` keeps the
inherited stdin for the live session.

```bash
agedum claude-deepseek-auto --run "review this" --dry-run
#   command
#     claude --print review this
```

## `--dry-run` { #dry-run }

Prints the full resolved launch without running it, so you can see exactly what context the
harness is given. It is grouped by **scope** (project / global); under each, every source
(`AGENTS.md`, `.agents/skills/`) is listed with its **disposition**: `→ <dest>` when
injected, `read in place` when the harness reads it natively, `→ kimi agent file` for the
kimi `--agent-file`, or an explicit note when a scope contributes nothing. Project-scope
paths display relative to the cwd; global-scope stays `~`-absolute. The resolved config (env
vars; opencode's `OPENCODE_CONFIG_CONTENT` pretty-printed; secrets masked) and the final
command are shown too. For a kimi provider run from a project root:

```text
provider   Kimi
harness    kimi
env file   ~/.config/agents/.env

project scope · ~/src/foo
  AGENTS.md         read in place (read natively — not injected)
  .agents/skills/   → .kimi/skills/

global scope
  ~/.config/agents/AGENTS.md   → kimi agent file (passed via --agent-file)
  ~/.config/agents/skills/     → ~/.kimi/skills/

command
  kimi
  + agedum appends: --agent-file /tmp/agedum-kimi-dry-…/agent.yaml
```

For an **opencode** provider the resolved config is shown as indented JSON (the
`environment` section), and a scope with no sources is stated explicitly, e.g.:

```text
environment
  OPENCODE_CONFIG_CONTENT
    {
      "model": "deepseek/deepseek-v4-pro",
      "provider": { "deepseek": { "models": { "deepseek-v4-pro": { "options": { "reasoningEffort": "max" } } } } }
    }

project scope · ~
  (no AGENTS.md or .agents/skills found here)

global scope
  ~/.config/agents/AGENTS.md   → ~/.config/opencode/AGENTS.md
  ~/.config/agents/skills/     → ~/.config/opencode/skills/
```

This is the same view [wrapper mode](wrapper.md#dry-run) shows — how
agedum renders the agent-neutral [source](source-shape.md) for the harness — plus the
resolved provider environment. Nothing is written to your real tree: the listed
destinations exist only inside the launched process's [mount namespace](internals.md).
