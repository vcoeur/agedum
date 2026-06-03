"""Locate and load the agent-neutral source shape for a project.

The source is the decided layout: a root ``AGENTS.md`` for instructions and
``.agents/skills/<name>/`` for skills. agedum compiles this per harness at launch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

AGENTS_MD = "AGENTS.md"
SKILLS_REL = Path(".agents") / "skills"


@dataclass(frozen=True)
class Source:
    """The resolved agent-neutral source for a project."""

    root: Path
    agents_md: Path | None
    skills_dir: Path | None


def find_project_root(start: Path | None = None) -> Path:
    """Nearest ancestor of `start` (incl. itself) holding `AGENTS.md`, `.agents/`,
    or `.git`. Falls back to `start` when none is found."""
    start = (start or Path.cwd()).resolve()
    for d in (start, *start.parents):
        if (d / AGENTS_MD).exists() or (d / ".agents").is_dir() or (d / ".git").exists():
            return d
    return start


def load_source(root: Path | None = None) -> Source:
    """Resolve the project root and the agent-neutral source files under it."""
    root = find_project_root(root)
    # $HOME can match ``find_project_root`` — it may hold ``~/.agents/`` (cline's global
    # ``AGENTS.md`` sink, or a legacy pre-XDG ``~/.agents/skills``) or be a git repo — but
    # home is never a project. Treating it as one would re-inject home-level files as
    # project scope (a duplicate bind). Yield an empty project source so only the global
    # scope (``~/.config/agents``) applies there.
    if root == Path.home().resolve():
        return Source(root=root, agents_md=None, skills_dir=None)
    agents = root / AGENTS_MD
    skills = root / SKILLS_REL
    return Source(
        root=root,
        agents_md=agents if agents.is_file() else None,
        skills_dir=skills if skills.is_dir() else None,
    )


def load_global_source() -> Source:
    """The global-scope source: `~/.config/agents/AGENTS.md` for instructions and
    `~/.config/agents/skills/` for skills (both XDG-aware, honouring `$XDG_CONFIG_HOME`).
    `root` is the home dir and is unused for mounting — global content is folded into the
    project injection."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_agents = (Path(xdg) if xdg else Path.home() / ".config") / "agents"
    agents = config_agents / AGENTS_MD
    skills = config_agents / "skills"
    return Source(
        root=Path.home(),
        agents_md=agents if agents.is_file() else None,
        skills_dir=skills if skills.is_dir() else None,
    )
