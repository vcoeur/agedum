---
title: aider harness · agedum
description: How agedum drives aider — wrapper-mode resolution (instructions injected via aider's --read read-only-context flag, no skills mechanism) and the provider config mapped to aider's CLI flags, with git integration disabled by default because agedum's launch namespace shares the real .git.
---

# aider

[aider](https://aider.chat) is a terminal pair-programming agent driven by litellm. It
differs from agedum's other harnesses in two ways that shape how agedum drives it:

- **No native instruction discovery.** aider reads neither `AGENTS.md` nor a `CONVENTIONS.md`
  on its own. Its one channel for standing context is `--read FILE` (a read-only file added
  to the chat) — the documented [conventions](https://aider.chat/docs/usage/conventions.html)
  mechanism. So agedum injects each scope's `AGENTS.md` as a `--read` argument, since aider
  reads no `AGENTS.md` natively.
- **No skills mechanism.** aider has nothing to load a skills tree into, so agedum **does not
  inject skills** for it (there is no `SKILL.aider.md` overlay). A project `.agents/skills/`
  shows up in `--dry-run` as `(not injected)`.

## Wrapper resolution { #wrapper-resolution }

| Source | Injected at |
|---|---|
| project `AGENTS.md` | `--read <compiled path>` (appended arg) |
| project `.agents/skills/` | *(not injected — aider has no skills mechanism)* |
| global `~/.config/agents/AGENTS.md` (+ optional `AGENTS.aider.md` overlay) | `--read <compiled path>` (appended arg) |
| global `~/.config/agents/skills/` | *(not injected — aider has no skills mechanism)* |

- **Instructions** — agedum compiles each scope's `AGENTS.md` to a file under the throwaway
  launch dir and appends `--read <path>` (project first, then global). The global one is the
  base merged with an optional `AGENTS.aider.md`
  [overlay](../source-shape.md#agentsharnessmd-per-harness-overlay-user-scope). Because the
  bwrap launch binds the whole real filesystem (`--dev-bind / /`), those paths resolve inside
  the namespace **without a dedicated bind** — `Plan.binds` stays empty, like kimi's agent
  file.
- `extra_args`: the `--read` flags above (one per scope that has an `AGENTS.md`).

```bash
agedum --wrapper aider -- aider                # drive aider with the same source
agedum --wrapper aider --dry-run -- aider      # show what would be injected
```

!!! warning "Wrapper mode does not disable git"
    Wrapper mode runs your **literal** command, so it does not append `--no-git`. agedum's
    launch namespace shares the real `.git`, and aider auto-commits by default — so to keep
    aider from committing into the real repo, pass `--no-git` yourself
    (`agedum --wrapper aider -- aider --no-git`) or use **provider mode**, which disables git
    by default (below).

## Provider config { #provider-config }

aider's knobs become **appended CLI flags** (like [Cline](cline.md) and [kimi](kimi.md)),
and CLI flags override any on-disk `.aider.conf.yml`:

```json
{
  "harness": "aider",
  "slug": "aider-deepseek",
  "secretEnv": "DEEPSEEK_API_KEY",
  "config": {
    "model": "deepseek/deepseek-chat",
    "reasoningEffort": "high"
  }
}
```

| `config` key | Appended |
|---|---|
| `model` | `--model <model>` |
| `weakModel` | `--weak-model <model>` (commit messages / history summarization) |
| `editorModel` | `--editor-model <model>` (the model used to apply edits) |
| `reasoningEffort` | `--reasoning-effort <level>` |
| `git` | **default `false`** → `--no-git`. `true` opts back into aider's git integration |
| `autoCommits` | when git is on, `false` → `--no-auto-commits` |
| `yesAlways` | `--yes-always` (true) — auto-confirm; useful with `--run` |
| `baseUrl` | sets `OPENAI_API_BASE` (env), for an OpenAI-compatible custom endpoint |

The config above launches `aider --model deepseek/deepseek-chat --reasoning-effort high
--no-git`.

**Git integration is disabled by default.** agedum's launch namespace shares the project's
**real, shared `.git`** (see [internals](../internals.md#no-git-tracked-targets)), and
aider's default is `--git` with `--auto-commits` — so left alone it would commit into the
real repo. agedum therefore appends **`--no-git`** unless the config sets `git: true`. With
git re-enabled you can still suppress commits with `autoCommits: false` (→ `--no-auto-commits`).
Enabling git in the shared namespace is a deliberate, hazardous opt-in.

**The API key rides the environment.** aider resolves credentials through litellm, which
reads provider-specific environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`DEEPSEEK_API_KEY`, …). agedum exports `secretEnv` (and any `requiredEnv`) into the child
under its own name via the normal [required-env](../provider.md#the-env-file) path, so no key
flag is appended and **no secret lands in argv**. Set `secretEnv` to the variable litellm
expects for your model's provider.

**Custom endpoints.** For an OpenAI-compatible endpoint, set `baseUrl` (→ `OPENAI_API_BASE`),
pick an `openai/<name>` model, and put the key in `OPENAI_API_KEY`:

```json
{
  "harness": "aider",
  "slug": "aider-local",
  "secretEnv": "OPENAI_API_KEY",
  "config": { "model": "openai/local-model", "baseUrl": "https://my.host/v1" }
}
```

**`--prompt`/`--run`.** aider's `--message "<text>"` runs a single message and exits (it
disables chat mode), which is `--run` (`aider --message "<text>"`). aider has no
"seed then stay interactive" mode, so `--prompt` is a fail-loud `ProviderError` — condash
then falls back to spawn-and-type. See the
[prompt-seeding table](../provider.md#prompt-seeding).
