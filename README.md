# agedum

> Latin *agedum* — "go on! / get going!"

Drive any agent CLI from an **agent-neutral source shape**, translating per harness
at launch. You keep one set of sources; agedum renders them for whichever agent CLI
you run.

- **Instructions** live in a root `AGENTS.md` (plain markdown).
- **Skills** live in `.agents/skills/<name>/` as `SKILL.md` (+ optional task files,
  scripts, and a per-harness `SKILL.<harness>.md` overlay).

agedum has two modes:

- **`agedum <provider-name|config.json> [harness args]`** — the primary form. Read a
  provider config JSON (a name resolved under `~/.config/agents/providers`, or a path),
  resolve its secrets from a `.env`, set the provider/model/auth environment, and launch
  the harness named in the config — inside the virtual-file context below. `--prompt
  "<text>"` seeds an initial prompt and stays interactive; `--run "<text>"` runs it
  non-interactively and exits. `--dry-run` prints the resolved env (secrets masked) + argv
  without launching.
- **`agedum --wrapper <harness> -- <command>`** — compile the source to the harness's
  native layout in a throwaway dir, then run your command inside a **private mount
  namespace** (bubblewrap) where the compiled files appear at their expected paths —
  visible only to that process, never written into your real tree or `$HOME`. For Claude:
  `AGENTS.md` → `CLAUDE.md` and `.agents/skills/<name>/` → `.claude/skills/<name>/` (the
  base `SKILL.md` merged with an optional `SKILL.claude.md` overlay). Provider mode runs
  this same launch after setting the environment.

> **Status:** Claude harness, **project + global scope**, implemented. Each scope
> lands at its *own* Claude location — project → `./CLAUDE.md` + `./.claude/skills/`,
> global (`~/.config/agents/AGENTS.md` + `~/.agents/skills/`) → `~/.claude/CLAUDE.md`
> + `~/.claude/skills/` (honours `$CLAUDE_CONFIG_DIR`). They're never merged; Claude
> reads both. Only those two `~/.claude` paths are overlaid for the child — your
> `~/.claude.json` auth and other settings are untouched.
>
> **kimi** (`--wrapper kimi`) is also supported. kimi reads the project `AGENTS.md`
> natively, so agedum leaves it in place; it has no user-scope `AGENTS.md`, so the global
> `AGENTS.md` is injected via a transient `--agent-file` YAML (no `--agent-file` is
> added when there's no global scope). Skills are binds: global → `~/.kimi/skills/`,
> project → `./.kimi/skills/` (both auto-read by kimi).
>
> **opencode** (`--wrapper opencode`) is supported too — pure path-discovery, like
> Claude. The project `AGENTS.md` is read natively (`./AGENTS.md`); the global
> `AGENTS.md` binds to `~/.config/opencode/AGENTS.md`; skills bind to `./.opencode/skills/`
> (project) and `~/.config/opencode/skills/` (global), both searched before
> `.agents/skills/` so the overlaid copy wins. No extra flags. Wrapper mode is Linux-only
> and requires `bwrap` on PATH.

## Usage

```bash
# Provider mode — launch a harness from a provider config, env resolved from .env:
agedum claude-deepseek-auto                       # resolve the named provider, launch claude
agedum claude-deepseek-auto -p "review this"      # extra args go to the harness
agedum claude-deepseek-auto --prompt "review this"  # seed an initial prompt, stay interactive
agedum claude-deepseek-auto --run "review this"     # run the prompt non-interactively, then exit
agedum ./providers/my-claude.json                 # a config path instead of a name
agedum claude-deepseek-auto --dry-run             # print resolved env, virtual files + argv

# Wrapper mode (low-level; provider mode builds on it) — virtual files, no provider env:
agedum --wrapper claude -- claude --model sonnet -p "review this"
agedum --wrapper claude --dry-run -- claude       # list what would be injected, don't run

agedum --version
```

`agedum <name>` is the normal way to launch. Wrapper mode is the lower-level entry it
uses: everything after `--` is the command, run verbatim, and `--wrapper <harness>`
chooses the format; `--dry-run` prints the injected virtual files without running.
Injected paths must be gitignored — agedum refuses to overlay a git-tracked file (the
namespace shares your real `.git`).

## Documentation

Full docs at **[agedum.vcoeur.com](https://agedum.vcoeur.com)**:

- [Source shape](https://agedum.vcoeur.com/source-shape/) — the structure of `AGENTS.md` and `.agents/skills/`
- [Scopes](https://agedum.vcoeur.com/scopes/) — project vs global (user) scope, and where each lands
- [Harnesses](https://agedum.vcoeur.com/harnesses/) — exactly what agedum does for each `--wrapper <harness>`
- [Provider mode](https://agedum.vcoeur.com/provider/) — launch a harness from a provider config JSON
- [CLI reference](https://agedum.vcoeur.com/cli/) and [Internals](https://agedum.vcoeur.com/internals/) — the mount-namespace launch and its safety rules

## Install

```bash
pipx install agedum        # standalone CLI (once published)
```

## Develop

```bash
make dev-install   # uv sync --all-groups
make test          # pytest
make lint          # ruff check + format --check
make run -- --version
make docs          # build the docs site (strict); docs-serve for live preview
```

Python ≥ 3.12, managed with [uv](https://docs.astral.sh/uv/). The version is
derived from the git tag (`vX.Y.Z`) at build time via hatch-vcs — never committed.

## Release

Tag the commit `vX.Y.Z` and push the tag; the `release` workflow builds and
publishes to PyPI via OIDC trusted publishing.

## License

MIT — see [LICENSE](LICENSE).
