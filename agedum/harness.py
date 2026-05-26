"""Compile the agent-neutral source into a harness's native on-disk layout.

Currently implements the Claude harness: ``AGENTS.md`` → ``CLAUDE.md`` and
``.agents/skills/<name>/`` → ``.claude/skills/<name>/`` (the base ``SKILL.md``
merged with an optional ``SKILL.claude.md`` overlay; task files and scripts copied
verbatim; other harnesses' overlays skipped).

The compiled tree lives in a throwaway directory; `Plan` says where each piece
should be mounted in the project view (see `agedum.launcher`).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agedum.sources import Source


@dataclass
class Plan:
    """How to present a compiled tree inside the project's mount view.

    `tmpfs` dirs are masked (ephemeral) so neither injected content nor the
    bwrap-created mountpoints leak onto the host; `binds` place compiled files at
    their project-relative targets.
    """

    tmpfs: list[str] = field(default_factory=list)
    binds: list[tuple[Path, str]] = field(default_factory=list)


def compile_claude(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the global + project source into Claude's layout under `dest`.

    Global scope is **folded into the project injection**: global instructions are
    prepended to the in-project `CLAUDE.md`, and global skills are placed alongside
    project skills under `.claude/skills/` (a project skill overrides a global one of
    the same name). Nothing is written into the user's real `~/.claude`.
    """
    plan = Plan()

    # Instructions: global AGENTS.md then project AGENTS.md -> one CLAUDE.md.
    parts: list[str] = []
    if global_ is not None and global_.agents_md is not None:
        parts.append(global_.agents_md.read_text())
    if project.agents_md is not None:
        parts.append(project.agents_md.read_text())
    if parts:
        claude_md = dest / "CLAUDE.md"
        claude_md.write_text("\n\n".join(p.strip("\n") for p in parts) + "\n")
        plan.binds.append((claude_md, "CLAUDE.md"))

    # Skills: global first, project second (project wins on name) -> .claude/skills/.
    skill_dirs: dict[str, Path] = {}
    for src in (global_, project):
        if src is None or src.skills_dir is None:
            continue
        for d in sorted(p for p in src.skills_dir.iterdir() if p.is_dir()):
            skill_dirs[d.name] = d
    if skill_dirs:
        skills_root = dest / ".claude" / "skills"
        for name, skill_dir in sorted(skill_dirs.items()):
            _compile_claude_skill(skill_dir, skills_root / name)
        # Mask `.claude` with a tmpfs, then bind the compiled skills inside it.
        plan.tmpfs.append(".claude")
        plan.binds.append((skills_root, ".claude/skills"))

    return plan


def _compile_claude_skill(src: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = (src / "SKILL.md").read_text() if (src / "SKILL.md").is_file() else ""
    overlay_path = src / "SKILL.claude.md"
    merged = base
    if overlay_path.is_file():
        merged = _merge_skill(base, overlay_path.read_text())
    (out / "SKILL.md").write_text(merged)

    # Copy task files / scripts / other assets; skip SKILL.md and any SKILL.<h>.md overlay.
    for item in src.iterdir():
        if item.name == "SKILL.md" or (item.name.startswith("SKILL.") and item.suffix == ".md"):
            continue
        dst = out / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)


def _merge_skill(base: str, overlay: str) -> str:
    """Merge a base `SKILL.md` (name/description) with a `SKILL.claude.md` overlay
    (harness-specific frontmatter + body). Overlay frontmatter keys win; bodies are
    concatenated."""
    base_meta, base_body = _split_frontmatter(base)
    over_meta, over_body = _split_frontmatter(overlay)
    meta = {**base_meta, **over_meta}
    body = base_body
    if over_body.strip():
        body = f"{base_body.rstrip()}\n\n{over_body.lstrip()}" if base_body.strip() else over_body
    return _emit_frontmatter(meta, body)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                meta = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, text[end + 4 :].lstrip("\n")
    return {}, text


def _emit_frontmatter(meta: dict, body: str) -> str:
    if not meta:
        return body
    fm = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{fm}\n---\n\n{body}"
