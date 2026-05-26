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

> **Status:** Claude harness, **project + global scope**, implemented. Global
> (`~/.config/agents/AGENTS.md` + `~/.agents/skills/`) is *folded into* the project
> injection — global instructions prepended to `CLAUDE.md`, global skills placed
> alongside project skills (project wins on name) — so nothing touches your real
> `~/.claude`. Other harnesses are follow-ups. Linux-only; requires `bwrap` on PATH.

## Usage

```bash
# Run any command with Claude-format virtual files injected from the project source:
agedum --claude -- claude --model sonnet -p "review this"
agedum --claude -- claude              # interactive

agedum --version
```

Everything after `--` is the command, run verbatim; the context flag before `--`
(`--claude`) chooses the format. The two are decoupled, so one context can front any
command. Injected paths must be gitignored — agedum refuses to overlay a git-tracked
file (the namespace shares your real `.git`).

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
```

Python ≥ 3.12, managed with [uv](https://docs.astral.sh/uv/). The version is
derived from the git tag (`vX.Y.Z`) at build time via hatch-vcs — never committed.

## Release

Tag the commit `vX.Y.Z` and push the tag; the `release` workflow builds and
publishes to PyPI via OIDC trusted publishing.

## License

MIT — see [LICENSE](LICENSE).
