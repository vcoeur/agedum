# agedum — operating instructions

A Python CLI that drives any agent CLI from an agent-neutral source shape
(`AGENTS.md` + `.agents/skills/`), compiling per harness and injecting it via a
private mount namespace at launch. Implemented: **Claude** and **kimi** harnesses at
**project + global scope**.

- **Claude** — each scope at its *own* location: project → `./CLAUDE.md` +
  `./.claude/skills/`; global (`~/.config/agents/AGENTS.md` + `~/.agents/skills/`) →
  `~/.claude/CLAUDE.md` + `~/.claude/skills/` (`$CLAUDE_CONFIG_DIR`-aware), never merged
  (only those two `~/.claude` paths overlaid; `~/.claude.json` auth untouched).
- **kimi** — flag/config-driven, so agedum *augments* the command: merged instructions
  → a `--agent-file` YAML; global skills → `~/.kimi/skills/` (bind); project skills →
  `--config extra_skill_dirs` (preserving `~/.kimi/config.toml`).

Follow-ups: opencode, `--<harness>-variant` composition.

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

`agedum <context-flags> -- <command...>`. Flag before `--` chooses the virtual-file
context (`--claude` / `--kimi`); everything after `--` is the child argv (some
harnesses also get extra flags appended — kimi's `--agent-file` / `--config`). Context
and command are decoupled; the flag space is open for future `--<harness>` modes.

Module layout: `sources.py` (locate the source), `harness.py` (`compile_claude` /
`compile_kimi` → a `Plan` of absolute binds **+ `extra_args`** for the command),
`launcher.py` (`build_bwrap_argv`, `assert_safe`, `run_virtualfs` — appends
`plan.extra_args`), `cli/main.py` (parse + `_COMPILERS` dispatch).

## Virtual-FS safety rules (validated empirically — don't regress)

- The namespace shares the **real `.git`**, so an in-namespace `git add`/`commit`
  writes to the real repo. `assert_safe` **refuses to inject over a git-tracked
  path**; injected targets must be untracked + gitignored.
- bwrap creates mountpoints on the real FS, leaving empty stubs after exit;
  `run_virtualfs` sweeps the ones it created (each target **and its parent**, deepest
  first, only if it didn't pre-exist). Plain `--ro-bind`s mask any pre-existing dir;
  injected content never leaks (leftovers are 0-byte / empty).
