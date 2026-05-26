"""Run a command inside a private mount namespace with injected virtual files.

agedum builds the harness's native layout in a throwaway dir, then runs the target
command under ``bwrap`` so those files appear at their expected paths for that
process (and its children) only — the real tree and ``$HOME`` are untouched. The
`Plan`'s binds are *absolute* targets, so the same mechanism places project-scope
files in the tree (``./CLAUDE.md``) and global-scope files under the user's Claude
dir (``~/.claude/...``). Harness-agnostic: every CLI just reads files.

Two safety rules, both validated empirically:

* An in-project target shares the *real, shared* ``.git`` of the project, so a
  `git add`/`commit` inside the namespace writes to the real repo. We **refuse to
  inject over a git-tracked path**; injected targets must be untracked + gitignored.
* bwrap creates mountpoints on the real filesystem, leaving empty (0-byte / empty
  dir) **stubs** after exit. We sweep the ones we created (a target or a parent dir
  that did not exist before the run).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agedum.harness import Plan


class LauncherError(RuntimeError):
    """A launch could not proceed safely (e.g. tracked target, or bwrap missing)."""


def build_bwrap_argv(plan: Plan, command: list[str]) -> list[str]:
    """Compose the ``bwrap`` argv: bind each compiled tree at its absolute target."""
    argv = ["bwrap", "--dev-bind", "/", "/"]
    for src, target in plan.binds:
        argv += ["--ro-bind", str(src), str(target)]
    argv += ["--", *command]
    return argv


def _git_tracked(project_root: Path, target: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--error-unmatch", target],
        capture_output=True,
    )
    return result.returncode == 0


def assert_safe(project_root: Path, plan: Plan) -> None:
    """Refuse to overlay a git-tracked path. Only in-project targets can be tracked;
    targets outside the project (e.g. ``~/.claude``) are not in this repo."""
    if not (project_root / ".git").exists():
        return
    for _, target in plan.binds:
        try:
            rel = target.relative_to(project_root)
        except ValueError:
            continue  # outside the project repo
        if _git_tracked(project_root, str(rel)):
            raise LauncherError(
                f"refusing to inject over git-tracked path '{rel}': it must be "
                f"untracked and gitignored (the namespace shares the real .git, so "
                f"injected content over a tracked file could be committed)."
            )


def _cleanup_candidates(plan: Plan) -> set[Path]:
    """Paths bwrap may have stubbed on the host: each target and its immediate parent
    (the parent covers a `.claude` dir created to hold a `skills` bind)."""
    candidates: set[Path] = set()
    for _, target in plan.binds:
        candidates.add(target)
        candidates.add(target.parent)
    return candidates


def run_virtualfs(project_root: Path, plan: Plan, command: list[str]) -> int:
    """Run `command` with `plan` injected; return its exit code. Sweeps stub mountpoints."""
    assert_safe(project_root, plan)
    argv = build_bwrap_argv(plan, command)
    candidates = _cleanup_candidates(plan)
    pre_existing = {p: p.exists() for p in candidates}
    try:
        return subprocess.run(argv).returncode
    except FileNotFoundError as exc:
        raise LauncherError(
            "bwrap (bubblewrap) is required for the virtual-FS launch but was not found "
            "on PATH — install it (e.g. `apt install bubblewrap`)."
        ) from exc
    finally:
        _sweep_stubs(candidates, pre_existing)


def _sweep_stubs(candidates: set[Path], pre_existing: dict[Path, bool]) -> None:
    # Deepest first, so a `.claude/skills` stub is removed before its `.claude` parent.
    for path in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        if pre_existing.get(path):
            continue  # was real before the run — never ours to remove
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
            elif path.is_file() and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass
