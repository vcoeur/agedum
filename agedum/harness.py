"""Compile the agent-neutral source into a harness's native on-disk layout.

Currently implements the Claude harness. Each scope is placed at *its own* Claude
location — they are never merged:

* **project** ``AGENTS.md`` → ``./CLAUDE.md``;
  ``.agents/skills/<n>/`` → ``./.claude/skills/<n>/``
* **global**  ``~/.config/agents/AGENTS.md`` → ``~/.claude/CLAUDE.md``;
  ``~/.agents/skills/<n>/`` → ``~/.claude/skills/<n>/``

Claude reads both scopes natively (user-scope `~/.claude/` + project-scope `./`), so
keeping them separate preserves the scope distinction. The compiled tree lives in a
throwaway directory; `Plan` records absolute (src → target) binds the launcher
mounts into the namespace.

Per skill: the base ``SKILL.md`` (minimal `name`/`description`) is merged with an
optional ``SKILL.claude.md`` overlay; task files and scripts are copied verbatim;
other harnesses' ``SKILL.<h>.md`` overlays are skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agedum.sources import Source


@dataclass
class Plan:
    """Absolute (compiled-source → mount-target) binds the launcher injects.

    Targets are absolute and may be inside the project (``./CLAUDE.md``) or under the
    user's Claude config dir (``~/.claude/...``) — that is how global vs project scope
    stay distinct.
    """

    binds: list[tuple[Path, Path]] = field(default_factory=list)
    # Extra args appended to the launched command (e.g. kimi's --agent-file / --config).
    extra_args: list[str] = field(default_factory=list)


def claude_config_dir() -> Path:
    """Claude's user-scope config dir — ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def compile_claude(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render each scope into its own Claude location under `dest`; return the `Plan`.

    Project → ``./CLAUDE.md`` + ``./.claude/skills``; global → ``~/.claude/CLAUDE.md``
    + ``~/.claude/skills``. The two are placed separately (never concatenated); Claude
    merges them at runtime.
    """
    plan = Plan()

    # Project scope -> in-tree Claude paths.
    _compile_scope(
        plan,
        project,
        dest / "project",
        claude_md_target=project.root / "CLAUDE.md",
        skills_target=project.root / ".claude" / "skills",
    )

    # Global scope -> the user-scope Claude config dir (separate from project).
    if global_ is not None:
        cc = claude_config_dir()
        _compile_scope(
            plan,
            global_,
            dest / "global",
            claude_md_target=cc / "CLAUDE.md",
            skills_target=cc / "skills",
        )

    return plan


def _compile_scope(
    plan: Plan,
    source: Source,
    dest: Path,
    *,
    claude_md_target: Path,
    skills_target: Path,
) -> None:
    if source.agents_md is not None:
        out = dest / "CLAUDE.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(source.agents_md.read_text())
        plan.binds.append((out, claude_md_target))

    if source.skills_dir is not None:
        skill_dirs = sorted(p for p in source.skills_dir.iterdir() if p.is_dir())
        if skill_dirs:
            skills_out = dest / "skills"
            for skill_dir in skill_dirs:
                _compile_skill(skill_dir, skills_out / skill_dir.name, "SKILL.claude.md")
            plan.binds.append((skills_out, skills_target))


def _compile_skill(src: Path, out: Path, overlay_name: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = (src / "SKILL.md").read_text() if (src / "SKILL.md").is_file() else ""
    overlay_path = src / overlay_name
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


# ---------------------------------------------------------------------------
# kimi-cli harness
# ---------------------------------------------------------------------------


def kimi_config_dir() -> Path:
    """kimi-cli's user-scope config dir (``~/.kimi``)."""
    return Path.home() / ".kimi"


def compile_kimi(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for kimi-cli. kimi is flag/config-driven (not pure
    path-discovery), so:

    * **instructions** (global + project ``AGENTS.md``, merged) → a transient
      ``--agent-file`` YAML appended to the command;
    * **global skills** → ``~/.kimi/skills/`` (kimi auto-merges them) via a bind;
    * **project skills** → a temp dir registered through ``--config`` (`extra_skill_dirs`).
    """
    plan = Plan()

    parts: list[str] = []
    if global_ is not None and global_.agents_md is not None:
        parts.append(global_.agents_md.read_text())
    if project.agents_md is not None:
        parts.append(project.agents_md.read_text())
    if parts:
        instructions = "\n\n".join(p.strip("\n") for p in parts) + "\n"
        agent_file = dest / "agent.yaml"
        agent_file.parent.mkdir(parents=True, exist_ok=True)
        agent_file.write_text(_kimi_agent_file_yaml(instructions))
        plan.extra_args += ["--agent-file", str(agent_file)]

    # Global skills -> ~/.kimi/skills (read by default; merge_all_available_skills).
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills")
        if out is not None:
            plan.binds.append((out, kimi_config_dir() / "skills"))

    # Project skills -> a temp dir registered via --config extra_skill_dirs.
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills")
        if out is not None:
            plan.extra_args += ["--config", _kimi_config_json([out])]

    return plan


def _compile_skill_tree(skills_dir: Path, out_root: Path) -> Path | None:
    """Compile each ``<skills_dir>/<name>/`` into ``out_root/<name>/`` for kimi;
    return ``out_root`` (or None when there are no skills)."""
    skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    if not skill_dirs:
        return None
    for skill_dir in skill_dirs:
        _compile_skill(skill_dir, out_root / skill_dir.name, "SKILL.kimi.md")
    return out_root


def _kimi_agent_file_yaml(instructions: str) -> str:
    """Wrap instructions into kimi's agent-file shape (extends the default agent,
    injecting the text as ``system_prompt_args.ROLE_ADDITIONAL``)."""
    indented = instructions.rstrip("\n").replace("\n", "\n      ")
    return (
        "version: 1\n"
        "agent:\n"
        "  extend: default\n"
        "  system_prompt_args:\n"
        "    ROLE_ADDITIONAL: |\n"
        f"      {indented}\n"
    )


def _kimi_config_json(extra_skill_dirs: list[Path]) -> str:
    """A kimi ``--config`` JSON that registers `extra_skill_dirs`, preserving the
    user's existing ``~/.kimi/config.toml`` (models / providers / auth) if present."""
    config: dict = {}
    cfg_file = kimi_config_dir() / "config.toml"
    if cfg_file.is_file():
        try:
            config = tomllib.loads(cfg_file.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            config = {}
    existing = config.get("extra_skill_dirs") or []
    config["extra_skill_dirs"] = [*existing, *(str(d) for d in extra_skill_dirs)]
    return json.dumps(config)
