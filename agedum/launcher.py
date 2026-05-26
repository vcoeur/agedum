"""Run a command inside a private mount namespace with injected virtual files.

agedum builds the harness's native layout in a throwaway dir, then runs the target
command under ``bwrap`` so those files appear at their expected project paths for
that process (and its children) only — the real tree and ``$HOME`` are untouched.
This is harness-agnostic: it works for any CLI, because every CLI just reads files.

Two safety rules, both validated empirically:

* The project's ``.git`` is the *real, shared* one, so a `git add`/`commit` run
  inside the namespace writes to the real repo. We therefore **refuse to inject
  over a git-tracked path** (injecting over a tracked file would let phantom content
  be committed). Injected targets must be untracked + gitignored.
* bwrap creates its mountpoints on the real filesystem, leaving empty (0-byte / empty
  dir) **stubs** after exit. We sweep the ones we created.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agedum.harness import Plan


class LauncherError(RuntimeError):
    """A launch could not proceed safely (e.g. tracked target, or bwrap missing)."""


def build_bwrap_argv(project_root: Path, plan: Plan, command: list[str]) -> list[str]:
    """Compose the ``bwrap`` argv. tmpfs masks come first so binds land inside them."""
    argv = ["bwrap", "--dev-bind", "/", "/"]
    for rel in plan.tmpfs:
        argv += ["--tmpfs", str(project_root / rel)]
    for src, target in plan.binds:
        argv += ["--ro-bind", str(src), str(project_root / target)]
    argv += ["--", *command]
    return argv


def _git_tracked(project_root: Path, target: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--error-unmatch", target],
        capture_output=True,
    )
    return result.returncode == 0


def assert_safe(project_root: Path, plan: Plan) -> None:
    """Refuse to overlay any git-tracked path (the real-`.git` hazard)."""
    if not (project_root / ".git").exists():
        return
    for target in (*plan.tmpfs, *(t for _, t in plan.binds)):
        if _git_tracked(project_root, target):
            raise LauncherError(
                f"refusing to inject over git-tracked path '{target}': it must be "
                f"untracked and gitignored (the namespace shares the real .git, so "
                f"injected content over a tracked file could be committed)."
            )


def _stub_paths(plan: Plan) -> list[str]:
    """Top-level mountpoints bwrap may stub on the host: tmpfs dirs + root-level file binds."""
    paths = list(plan.tmpfs)
    paths += [t for _, t in plan.binds if "/" not in t]
    return paths


def run_virtualfs(project_root: Path, plan: Plan, command: list[str]) -> int:
    """Run `command` with `plan` injected; return its exit code. Sweeps stub mountpoints."""
    assert_safe(project_root, plan)
    argv = build_bwrap_argv(project_root, plan, command)
    stubs = _stub_paths(plan)
    pre_existing = {p: (project_root / p).exists() for p in stubs}
    try:
        return subprocess.run(argv).returncode
    except FileNotFoundError as exc:
        raise LauncherError(
            "bwrap (bubblewrap) is required for the virtual-FS launch but was not found "
            "on PATH — install it (e.g. `apt install bubblewrap`)."
        ) from exc
    finally:
        _sweep_stubs(project_root, stubs, pre_existing)


def _sweep_stubs(project_root: Path, stubs: list[str], pre_existing: dict[str, bool]) -> None:
    for rel in stubs:
        if pre_existing.get(rel):
            continue  # was real before the run — never ours to remove
        path = project_root / rel
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
            elif path.is_file() and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass
