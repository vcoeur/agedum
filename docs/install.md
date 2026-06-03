---
title: Install · agedum
description: Install agedum from PyPI, the runtime prerequisites (bubblewrap, Linux), and the developer setup with uv.
---

# Install

```bash
pipx install agedum
# or
uv tool install agedum
```

Both install `agedum` into its own isolated virtualenv and put it on your `$PATH`.

## Prerequisites

- **Linux.** agedum launches your command inside a private mount namespace, which is a
  Linux kernel feature. macOS and Windows are not supported.
- **`bwrap` on `PATH`.** The namespace is created with
  [bubblewrap](https://github.com/containers/bubblewrap). Install it from your distro:

    ```bash
    sudo apt install bubblewrap     # Debian / Ubuntu
    sudo dnf install bubblewrap     # Fedora
    sudo pacman -S bubblewrap       # Arch
    ```

    If `bwrap` is missing at launch, agedum exits with a clear error rather than
    silently running your command with no injected context.

- **Python ≥ 3.12** — only relevant if you install from source; `pipx` / `uv tool`
  manage their own interpreter.

The agent CLI you intend to drive (`claude`, `kimi`, …) must already be installed and
on `PATH` — agedum runs it, it does not bundle it.

## First run

agedum needs no configuration file. On launch it looks for an agent-neutral
[source](source-shape.md):

- **Project** — the nearest ancestor of the current directory holding `AGENTS.md`,
  `.agents/`, or `.git`.
- **Global** — `~/.config/agents/AGENTS.md` (honours `$XDG_CONFIG_HOME`) and
  `~/.agents/skills/`.

If neither scope has any `AGENTS.md` or skills, agedum prints a warning and runs your
command with nothing injected. See [Source & scopes](source-shape.md#scopes) for the full resolution rules.

!!! warning "Injected paths must be gitignored"
    The mount namespace shares your **real** `.git`. agedum refuses to overlay a
    git-tracked file, so the targets it injects (`CLAUDE.md`, `.claude/`, `.kimi/`)
    must be untracked and listed in `.gitignore`. See [Internals](internals.md#safety).

## Develop

```bash
git clone https://github.com/vcoeur/agedum
cd agedum
make dev-install   # uv sync --all-groups
make test          # uv run pytest
make lint          # ruff check + ruff format --check
make run -- --version
```

agedum is a [uv](https://docs.astral.sh/uv/)-managed project (Python ≥ 3.12). The
version is derived from the git tag (`vX.Y.Z`) at build time via
[hatch-vcs](https://github.com/ofek/hatch-vcs) — never committed. A source tree with
no tag resolves to a dev version. Commit `uv.lock`; `.venv/` stays gitignored.

### Build the docs locally

```bash
make docs          # build the static site into site/ (strict)
make docs-serve    # live-reload preview at http://127.0.0.1:8000
```

## Release

Tag the commit `vX.Y.Z` and push the tag; the `release` workflow builds the wheel and
publishes it to [PyPI](https://pypi.org/project/agedum/) via OIDC trusted publishing
(no token in the repo). Tag only after merge.
