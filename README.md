# agedum

> Latin *agedum* — "go on! / get going!"

Drive any agent CLI from an **agent-neutral source shape**, translating per harness
at launch. You keep one set of sources; agedum renders them for whichever agent CLI
you run.

- **Instructions** live in a root `AGENTS.md` (plain markdown).
- **Skills** live in `.agents/skills/<name>/` as `SKILL.md` (+ optional task files,
  scripts, and a per-harness `SKILL.<harness>.md` overlay).

At launch, agedum reads that shape (project scope, plus a global scope under
`~/.config/agents/AGENTS.md` and `~/.agents/skills/`) and places/translates it for
the active harness — for Claude this is mostly placement (`.claude/skills/`,
`CLAUDE.md`); other harnesses get format translation, with frontmatter they don't
understand stripped and scripts they can't run dropped — then launches the CLI.

> **Status: scaffold.** The CLI surface and packaging are in place; the
> resolve/translate/exec pipeline is not implemented yet.

## Usage

```bash
agedum               # launch interactively (terminal)
agedum --run "..."   # run a one-shot task
agedum --version
```

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
