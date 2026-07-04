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

import glob
import os
import subprocess
from pathlib import Path

from agedum.harness import Plan, Sandbox

# The token a sandbox ``read_write`` template expands to the project root.
PROJECT_ROOT_TOKEN = "${PROJECT_ROOT}"


class LauncherError(RuntimeError):
    """A launch could not proceed safely (e.g. tracked target, or bwrap missing)."""


def _effective_binds(plan: Plan) -> list[tuple[Path, Path]]:
    """Expand each *directory* bind one level so it overlays rather than masks its target.

    A whole-dir ``--ro-bind src target`` replaces the entire view of ``target``, hiding
    anything already there. The only directory binds agedum emits are skill trees, and we
    want to inject exactly the skills agedum ships while leaving hand-authored skills
    already in the target dir (``~/.config/opencode/skills/``, ``~/.claude/skills/``, …)
    visible. Binding each child at ``target/<child>`` does that: agedum's skill folders
    replace any same-named on-disk folder, but unrelated siblings show through the
    underlying real dir. File binds (CLAUDE.md / AGENTS.md) pass through unchanged.
    """
    expanded: list[tuple[Path, Path]] = []
    for src, target in plan.binds:
        if src.is_dir():
            expanded += [(child, target / child.name) for child in sorted(src.iterdir())]
        else:
            expanded.append((src, target))
    return expanded


def _resolve_rw(raw: str, project_root: Path) -> list[Path]:
    """Resolve a sandbox ``read_write`` template to absolute paths.

    Expands ``${PROJECT_ROOT}`` to the project root, ``$VAR`` from the environment, and a
    leading ``~`` to the home dir. If the result holds a shell glob metacharacter
    (``*`` / ``?`` / ``[``) it is expanded against the filesystem and **every existing match**
    is returned, sorted (``~/src/*`` → each immediate child of ``~/src``); an unmatched glob
    contributes nothing. A plain (non-glob) template returns its single literal path whether or
    not it exists, matching the pre-glob behaviour."""
    expanded = raw.replace(PROJECT_ROOT_TOKEN, str(project_root))
    resolved = str(Path(os.path.expandvars(expanded)).expanduser())
    if any(char in resolved for char in "*?["):
        return [Path(match) for match in sorted(glob.glob(resolved))]
    return [Path(resolved)]


def _nearest_existing_dir(path: Path) -> Path:
    """The deepest existing ancestor of ``path`` (or ``path`` itself if it exists).

    bwrap cannot create a mount point on a read-only parent, so an injected file's nearest
    existing ancestor is what must be made writable for the bind to land."""
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def writable_roots(plan: Plan, sandbox: Sandbox, project_root: Path) -> list[Path]:
    """The directories a write-confinement launch mounts read-write.

    The union of four sources, de-duplicated with descendants dropped (a parent bind
    already makes them writable):

    * the **project root** — the agent's working tree, and where project-scope files are
      injected;
    * the **nearest existing ancestor of every injected file** — so bwrap can create the
      mount point (it cannot on a read-only parent);
    * the **harness's own state/config dir(s)** (``plan.writable_dirs``) — so it can persist
      sessions/settings/auth (e.g. ``~/.cline`` for Cline, ``~/.claude`` for Claude Code)
      whether or not an injection happens to land under it (:func:`run_virtualfs` creates any
      that are missing so the bind can land);
    * each ``sandbox.read_write`` entry, resolved (and glob-expanded) to zero or more paths —
      extra data the agent may modify.

    ``/tmp`` is writable separately (a private tmpfs), not via this list.
    """
    roots: list[Path] = [project_root]
    for _, target in _effective_binds(plan):
        roots.append(_nearest_existing_dir(target.parent))
    roots += plan.writable_dirs
    for raw in sandbox.read_write:
        roots += _resolve_rw(raw, project_root)
    return _dedupe_roots(roots)


def _dedupe_roots(paths: list[Path]) -> list[Path]:
    """Drop duplicates and any path nested under another in the list, keeping first-seen
    order — a parent ``--bind`` already covers everything beneath it."""
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return [p for p in seen if not any(p != other and p.is_relative_to(other) for other in seen)]


def build_bwrap_argv(
    plan: Plan,
    command: list[str],
    *,
    sandbox: Sandbox | None = None,
    project_root: Path | None = None,
) -> list[str]:
    """Compose the ``bwrap`` argv: bind each compiled tree at its absolute target.

    Without an enabled ``sandbox`` (the default) the whole host is bound **read-write**
    (``--dev-bind / /``) and only the injected files are read-only — the namespace isolates
    *what the harness reads as config*, not the filesystem. With an enabled ``sandbox``
    (write-confinement) the host is bound **read-only**, only :func:`writable_roots` (plus a
    private ``/tmp``) are writable, and ``--dev`` / ``--proc`` supply the device and process
    mounts a read-only root would otherwise lack — so the harness cannot modify files
    outside its working set. ``project_root`` is required when a sandbox is enabled (it seeds
    the writable set and resolves ``${PROJECT_ROOT}``).

    Directory binds are overlaid per-child (see :func:`_effective_binds`) so a skills bind
    adds agedum's skills without erasing hand-authored ones already in the target dir.
    ``safe_overrides`` are tmpfs-shadowed — an empty mount hides the real path without
    touching disk."""
    if sandbox is not None and sandbox.enabled:
        if project_root is None:
            raise LauncherError("a sandbox launch requires the project root")
        argv = [
            "bwrap", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        ]  # fmt: skip
        for writable in writable_roots(plan, sandbox, project_root):
            argv += ["--bind", str(writable), str(writable)]
    else:
        argv = ["bwrap", "--dev-bind", "/", "/"]
    for override_target in sorted(plan.safe_overrides):
        argv += ["--tmpfs", str(override_target)]
    for src, target in _effective_binds(plan):
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
    targets outside the project (e.g. ``~/.claude``) are not in this repo.

    The check runs over the *effective* (per-child) binds, not the raw dir-level ones:
    a skills dir is overlaid per-child (see :func:`_effective_binds`), so a tracked but
    unrelated sibling (e.g. a hand-authored skill versioned in the repo) is never masked
    and must not block the launch — only a target agedum would actually bind over does.
    ``safe_overrides`` are not checked: they are read-only tmpfs shadows, never content
    injections, and never appear in ``plan.binds``."""
    if not (project_root / ".git").exists():
        return
    for _, target in _effective_binds(plan):
        try:
            rel = target.relative_to(project_root)
        except ValueError:
            continue  # outside the project repo
        if _git_tracked(project_root, str(rel)):
            raise LauncherError(
                f"refusing to inject over git-tracked path '{rel}': it must be "
                f"untracked and gitignored (the namespace shares the real .git, so "
                f"injected content over a tracked file could be committed). Untrack it "
                f"first — `git rm --cached '{rel}'` — and add it to .gitignore."
            )


def _cleanup_candidates(plan: Plan) -> set[Path]:
    """Paths bwrap may have stubbed on the host. We union two levels because skills binds
    are overlaid per-child:

    * **dir-level** (``plan.binds``) — a skills target ``.claude/skills`` and its parent
      ``.claude``: the dirs bwrap may create to hold the bind;
    * **per-child** (:func:`_effective_binds`) — ``.claude/skills/<name>``: the empty stub
      bwrap leaves for an overlaid skill that has no on-disk counterpart.

    ``safe_overrides`` (tmpfs shadows) are candidates too: bwrap creates their mountpoint
    dirs just like bind targets, so shadowing a path that does not exist (e.g. pi's
    ``.agents/skills`` in a project without one) would otherwise leave a stub behind.

    The sweep is deepest-first and skips anything that pre-existed, so listing a real dir
    (e.g. a user's ``~/.config/opencode/skills`` that already held skills) is harmless — it
    is never removed."""
    candidates: set[Path] = set()
    for target in (
        *(target for _, target in (*plan.binds, *_effective_binds(plan))),
        *plan.safe_overrides,
    ):
        candidates.add(target)
        candidates.add(target.parent)
    return candidates


def _ensure_writable_dirs(plan: Plan) -> None:
    """Create each harness state/config dir so its read-write bind can land.

    bwrap cannot ``--bind`` a path that does not exist, so a harness whose state dir has never
    been created (a fresh machine) would fail to mount it writable. The harness would create
    its own config dir on first run anyway, so pre-creating it is benign. Only called on the
    real-run path under an enabled sandbox — never from :func:`writable_roots`, so ``--dry-run``
    stays side-effect-free."""
    for path in plan.writable_dirs:
        path.mkdir(parents=True, exist_ok=True)


def run_virtualfs(
    project_root: Path,
    plan: Plan,
    command: list[str],
    *,
    close_stdin: bool = False,
    sandbox: Sandbox | None = None,
) -> int:
    """Run `command` with `plan` injected; return its exit code. Sweeps stub mountpoints.

    ``close_stdin`` redirects the child's stdin to ``/dev/null``. A non-interactive
    ``--run`` reads its whole task from argv and must never block waiting on input it will
    never get — opencode's ``run`` subcommand otherwise hangs forever on an open, non-tty
    stdin (e.g. a pipe). Interactive launches (a bare provider or ``--prompt``) keep the
    inherited stdin so the live session can read keystrokes.

    ``sandbox`` (when enabled) switches the launch to write-confinement: the host is mounted
    read-only and only the working set is writable (see :func:`build_bwrap_argv`).
    """
    assert_safe(project_root, plan)
    if sandbox is not None and sandbox.enabled:
        _ensure_writable_dirs(plan)
    argv = build_bwrap_argv(
        plan, [*command, *plan.extra_args], sandbox=sandbox, project_root=project_root
    )
    candidates = _cleanup_candidates(plan)
    pre_existing = {p: p.exists() for p in candidates}
    stdin = subprocess.DEVNULL if close_stdin else None
    try:
        return subprocess.run(argv, stdin=stdin).returncode
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
