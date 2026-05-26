# agedum — operating instructions

A Python CLI that drives any agent CLI from an agent-neutral source shape
(`AGENTS.md` + `.agents/skills/`), compiling per harness and injecting it via a
private mount namespace at launch. Implemented: the **Claude** harness at **project +
global scope**, each placed at its *own* Claude location — project → `./CLAUDE.md` +
`./.claude/skills/`; global (`~/.config/agents/AGENTS.md` + `~/.agents/skills/`) →
`~/.claude/CLAUDE.md` + `~/.claude/skills/` (`$CLAUDE_CONFIG_DIR`-aware), never merged
(only those two `~/.claude` paths are overlaid; `~/.claude.json` auth is untouched).
Follow-ups: other harnesses, variant composition.

## Stack

- Python ≥ 3.12, managed with **uv** (`uv sync`, `uv run`). Don't use raw pip/venv.
- **Manual `argv` parsing** (not Typer) so everything after `--` is opaque
  passthrough; Rich for stderr output. Entry point: `agedum.cli.main:app`
  (`[project.scripts]`). Deps: `pyyaml` (skill frontmatter merge), `rich`.
- Runtime dep: **`bwrap`** (bubblewrap) on PATH for the virtual-FS launch. Linux-only.
- Flat package layout: the `agedum/` package sits at the repo root (no `src/`).
- Version is **dynamic via hatch-vcs** — derived from the git tag `vX.Y.Z` at build
  time, never committed. A source tree with no tag resolves to a dev version;
  `agedum.__version__` falls back to `0.0.0` when the package isn't installed.

## Commands

```bash
make dev-install   # uv sync --all-groups
make test          # uv run pytest
make lint          # ruff check + ruff format --check
make format        # ruff --fix + format
make run -- --version
```

Run `make format` after every change. Commit `uv.lock`; `.venv/` stays gitignored.

## CI / release

- `.github/workflows/ci.yml` — ruff lint + format-check + pytest on push to `main`
  and every PR.
- `.github/workflows/release.yml` — on a `v*` tag push, `uv build` then publish to
  **PyPI** via OIDC trusted publishing (no token in the repo). Tag only after merge.

## CLI contract

`agedum <context-flags> -- <command...>`. Flags before `--` choose the virtual-file
context (`--claude`); everything after `--` is the child argv, run verbatim inside
the namespace. Context and command are decoupled (one context can front any
command); the flag space is open for future `--<harness>` modes and variants.

Module layout: `sources.py` (locate `AGENTS.md` + `.agents/skills/`), `harness.py`
(`compile_claude` → native layout + a mount `Plan`), `launcher.py` (`build_bwrap_argv`,
`assert_safe`, `run_virtualfs`), `cli/main.py` (parse + dispatch).

## Virtual-FS safety rules (validated empirically — don't regress)

- The namespace shares the **real `.git`**, so an in-namespace `git add`/`commit`
  writes to the real repo. `assert_safe` **refuses to inject over a git-tracked
  path**; injected targets must be untracked + gitignored.
- bwrap creates mountpoints on the real FS, leaving empty stubs after exit;
  `run_virtualfs` sweeps the ones it created. tmpfs-mask injected *dirs* so their
  contents can never leak (only an empty stub dir remains, which is swept).
