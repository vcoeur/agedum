# agedum

> Latin *agedum* — "go on! / get going!"

Drive any agent CLI from an **agent-neutral source shape**, translating per harness
at launch. You keep one set of sources; agedum renders them for whichever agent CLI
you run.

- **Instructions** live in a root `AGENTS.md` (plain markdown).
- **Skills** live in `.agents/skills/<name>/` as `SKILL.md` (+ optional task files,
  scripts, and a per-harness `SKILL.<harness>.md` overlay).

At launch, agedum compiles that shape to the harness's native layout in a throwaway
dir, then runs your command inside a **private mount namespace** (bubblewrap) where
the compiled files appear at their expected paths — visible only to that process,
never written into your real tree or `$HOME`. For Claude: `AGENTS.md` → `CLAUDE.md`
and `.agents/skills/<name>/` → `.claude/skills/<name>/` (the base `SKILL.md` merged
with an optional `SKILL.claude.md` overlay).

> **Status:** Claude harness, **project + global scope**, implemented. Each scope
> lands at its *own* Claude location — project → `./CLAUDE.md` + `./.claude/skills/`,
> global (`~/.config/agents/AGENTS.md` + `~/.agents/skills/`) → `~/.claude/CLAUDE.md`
> + `~/.claude/skills/` (honours `$CLAUDE_CONFIG_DIR`). They're never merged; Claude
> reads both. Only those two `~/.claude` paths are overlaid for the child — your
> `~/.claude.json` auth and other settings are untouched.
>
> **kimi** (`--kimi`) is also supported. Instructions can only be injected via a
> flag, so agedum *augments* the command: merged global+project `AGENTS.md` → a
> transient `--agent-file` YAML. Skills are binds: global → `~/.kimi/skills/`,
> project → `./.kimi/skills/` (both auto-read by kimi). Other harnesses (opencode)
> are follow-ups. Linux-only; requires `bwrap` on PATH.

## Usage

```bash
# Run a command with virtual files injected from the project + global source:
agedum --claude -- claude --model sonnet -p "review this"
agedum --claude -- claude              # interactive
agedum --kimi   -- kimi -p "explain this code"

agedum --version
```

Everything after `--` is the command, run verbatim; the context flag before `--`
(`--claude`) chooses the format. The two are decoupled, so one context can front any
command. Injected paths must be gitignored — agedum refuses to overlay a git-tracked
file (the namespace shares your real `.git`).

## Documentation

Full docs at **[agedum.vcoeur.com](https://agedum.vcoeur.com)**:

- [Source shape](https://agedum.vcoeur.com/source-shape/) — the structure of `AGENTS.md` and `.agents/skills/`
- [Scopes](https://agedum.vcoeur.com/scopes/) — project vs global (user) scope, and where each lands
- [Harnesses](https://agedum.vcoeur.com/harnesses/) — exactly what agedum does for each `--<harness>` command
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
