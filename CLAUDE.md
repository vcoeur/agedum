# agedum — operating instructions

A Python CLI that drives any agent CLI from an agent-neutral source shape
(`AGENTS.md` + `.agents/skills/`), translating per harness at launch. **Currently a
scaffold** — the resolve/translate/exec pipeline is not implemented yet.

## Stack

- Python ≥ 3.12, managed with **uv** (`uv sync`, `uv run`). Don't use raw pip/venv.
- CLI built with **Typer** (+ Rich for output). Entry point: `agedum.cli.main:app`
  (`[project.scripts]`).
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

## The intended CLI contract

agedum is invoked as the agent binary: `agedum` (interactive launch) or
`agedum --run "<PROMPT>"` (one-shot task). Keep that surface stable — it is the
contract the calling launcher/dashboard drives.
