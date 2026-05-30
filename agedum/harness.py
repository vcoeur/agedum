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

Instructions get the same overlay treatment, but **only at user (global) scope**: the
base ``~/.config/agents/AGENTS.md`` is merged with an optional sibling
``~/.config/agents/AGENTS.<harness>.md`` (see :func:`_instructions`). ``AGENTS.md``
carries no front-matter, so the merge is a plain body concatenation.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
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
    # Extra args appended to the launched command (e.g. kimi's --agent-file).
    extra_args: list[str] = field(default_factory=list)
    # Provenance for --dry-run: injected dest path -> the agent-neutral source it came
    # from (a bind target, or kimi's --agent-file path). Display-only; the launcher
    # ignores it.
    origins: dict[Path, str] = field(default_factory=dict)
    # Sources the harness reads *in place* without a bind (kimi/opencode read the project
    # AGENTS.md natively). Display-only for --dry-run, so they are not invisible.
    native_reads: list[Path] = field(default_factory=list)


def claude_config_dir() -> Path:
    """Claude's user-scope config dir — ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _instructions(source: Source, overlay_harness: str | None) -> str | None:
    """Resolve a scope's instructions text, optionally merging a harness overlay.

    Returns the scope's ``AGENTS.md`` text, or ``None`` when the scope has no base
    ``AGENTS.md``. When ``overlay_harness`` is a harness name and a sibling
    ``AGENTS.<harness>.md`` exists next to the base, the two are merged
    (:func:`_merge_instructions`); when it is ``None`` the base text is returned
    verbatim. Used with an overlay only at user (global) scope.
    """
    if source.agents_md is None:
        return None
    base = source.agents_md.read_text()
    if overlay_harness is None:
        return base
    overlay = source.agents_md.parent / f"AGENTS.{overlay_harness}.md"
    if overlay.is_file():
        return _merge_instructions(base, overlay.read_text())
    return base


def _merge_instructions(base: str, overlay: str) -> str:
    """Merge a base ``AGENTS.md`` with a harness-specific ``AGENTS.<harness>.md`` overlay.

    Unlike ``SKILL.md``, ``AGENTS.md`` carries no front-matter (it is raw-injected into
    the prompt), so there is nothing to union — the bodies are concatenated with a blank
    line between them. A non-empty part survives an empty counterpart.
    """
    if not overlay.strip():
        return base
    if not base.strip():
        return overlay
    return f"{base.rstrip()}\n\n{overlay.lstrip()}\n"


def _claude_emitter_path() -> str:
    """Absolute path of the bundled Claude transcript-capture hook script.

    Shipped inside the agedum package (``agedum/assets/claude/emit-transcript.mjs``)
    and resolved on disk; agedum's bwrap launch binds the whole real filesystem, so
    the path is visible to Claude's hook subprocess inside the namespace.
    """
    return str(Path(__file__).resolve().parent / "assets" / "claude" / "emit-transcript.mjs")


def _transcript_hook_settings() -> dict:
    """Claude ``settings.json`` ``hooks`` block that emits the session transcript.

    ``UserPromptSubmit`` frames the prompt as a ``[user]`` message; ``Stop`` tails
    the session JSONL after each assistant turn and frames the new assistant +
    thinking content. Both speak the neutral OSC 7373 protocol condash decodes (the
    same one the opencode plugin emits) — see ``assets/claude/emit-transcript.mjs``.
    """
    emitter = shlex.quote(_claude_emitter_path())
    return {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"node {emitter} user"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": f"node {emitter} stop"}]}],
    }


def _inject_transcript_settings(plan: Plan, project_root: Path, proj_dest: Path) -> None:
    """Bind a project ``.claude/settings.json`` that adds transcript-capture hooks.

    Merged into the project's existing ``settings.json`` (preserving its keys and
    any hooks it already declares) so the bind never clobbers a project's own
    config. The user-scope ``~/.claude/settings.json`` is a separate layer Claude
    merges at runtime, so the user's own hooks are untouched either way.
    """
    target = project_root / ".claude" / "settings.json"
    existing: dict = {}
    try:
        loaded = json.loads(target.read_text())
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        existing = {}

    merged = dict(existing)
    hooks = dict(existing.get("hooks") or {})
    for event, entries in _transcript_hook_settings().items():
        hooks[event] = list(hooks.get(event) or []) + entries
    merged["hooks"] = hooks

    out = proj_dest / ".claude" / "settings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=2) + "\n")
    plan.binds.append((out, target))
    plan.origins[target] = f"{_claude_emitter_path()} (transcript hooks)"


def compile_claude(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render each scope into its own Claude location under `dest`; return the `Plan`.

    Project → ``./CLAUDE.md`` + ``./.claude/skills``; global → ``~/.claude/CLAUDE.md``
    + ``~/.claude/skills``. The two are placed separately (never concatenated); Claude
    merges them at runtime.
    """
    plan = Plan()

    # Project scope -> in-tree Claude paths. No instructions overlay (user scope only).
    _compile_scope(
        plan,
        project,
        dest / "project",
        instructions=_instructions(project, None),
        claude_md_target=project.root / "CLAUDE.md",
        skills_target=project.root / ".claude" / "skills",
    )

    # Emit a clean session transcript for any pty capturer (condash) by binding a
    # project settings.json that registers the transcript-capture hooks. Default on,
    # mirroring opencode; the OSC it emits is ignored by terminals that don't decode it.
    # Gated on an actual project source so a sourceless launch stays fully transparent
    # (no binds) and never injects into an unrelated directory.
    if project.agents_md is not None or project.skills_dir is not None:
        _inject_transcript_settings(plan, project.root, dest / "project")

    # Global scope -> the user-scope Claude config dir (separate from project), with the
    # AGENTS.claude.md overlay merged into the global instructions.
    if global_ is not None:
        cc = claude_config_dir()
        _compile_scope(
            plan,
            global_,
            dest / "global",
            instructions=_instructions(global_, "claude"),
            claude_md_target=cc / "CLAUDE.md",
            skills_target=cc / "skills",
        )

    return plan


def _compile_scope(
    plan: Plan,
    source: Source,
    dest: Path,
    *,
    instructions: str | None,
    claude_md_target: Path,
    skills_target: Path,
) -> None:
    if instructions is not None:
        out = dest / "CLAUDE.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(instructions)
        plan.binds.append((out, claude_md_target))
        if source.agents_md is not None:
            plan.origins[claude_md_target] = str(source.agents_md)

    if source.skills_dir is not None:
        skill_dirs = sorted(p for p in source.skills_dir.iterdir() if p.is_dir())
        if skill_dirs:
            skills_out = dest / "skills"
            for skill_dir in skill_dirs:
                _compile_skill(skill_dir, skills_out / skill_dir.name, "SKILL.claude.md")
            plan.binds.append((skills_out, skills_target))
            plan.origins[skills_target] = str(source.skills_dir)


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
    """Render the source for kimi-cli.

    kimi reads the **project** ``AGENTS.md`` natively — it merges every ``AGENTS.md``
    from the project root (nearest ``.git``) down to the work dir into the system
    prompt's ``KIMI_AGENTS_MD`` slot — so the source file is already where kimi looks
    and agedum injects nothing for it. kimi has **no user-scope ``AGENTS.md``**, so the
    **global** ``AGENTS.md`` (base merged with an optional ``AGENTS.kimi.md`` overlay) is
    injected via a custom ``--agent-file`` that extends the default agent
    (``system_prompt_args.ROLE_ADDITIONAL``). The two coexist: the
    agent-file fills ``ROLE_ADDITIONAL`` while native discovery fills ``KIMI_AGENTS_MD``.

    Skills are binds:

    * **global skills** → ``~/.kimi/skills/`` (kimi auto-merges them);
    * **project skills** → ``./.kimi/skills/`` (project-local; kimi auto-reads it).
    """
    plan = Plan()

    # Project AGENTS.md is read natively from ./AGENTS.md (no bind) — record it so
    # --dry-run can show it rather than leave it invisible.
    if project.agents_md is not None:
        plan.native_reads.append(project.agents_md)

    # Global instructions (base + AGENTS.kimi.md overlay) -> a custom --agent-file (kimi
    # has no user-scope AGENTS.md). Project instructions are read natively from
    # ./AGENTS.md, so they are left in place.
    global_instructions = _instructions(global_, "kimi") if global_ is not None else None
    if global_instructions is not None:
        instructions = global_instructions.strip("\n") + "\n"
        agent_file = dest / "agent.yaml"
        agent_file.parent.mkdir(parents=True, exist_ok=True)
        agent_file.write_text(_kimi_agent_file_yaml(instructions))
        plan.extra_args += ["--agent-file", str(agent_file)]
        if global_ is not None and global_.agents_md is not None:
            plan.origins[agent_file] = str(global_.agents_md)

    # Global skills -> ~/.kimi/skills (read by default; merge_all_available_skills).
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills", "SKILL.kimi.md")
        if out is not None:
            target = kimi_config_dir() / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(global_.skills_dir)

    # Project skills -> ./.kimi/skills (project-local; kimi auto-reads it, matching
    # condash's prior layout — uniform with the Claude harness, no config rewrite).
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills", "SKILL.kimi.md")
        if out is not None:
            target = project.root / ".kimi" / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(project.skills_dir)

    return plan


def _compile_skill_tree(skills_dir: Path, out_root: Path, overlay_name: str) -> Path | None:
    """Compile each ``<skills_dir>/<name>/`` into ``out_root/<name>/``, applying the
    ``overlay_name`` overlay; return ``out_root`` (or None when there are no skills)."""
    skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    if not skill_dirs:
        return None
    for skill_dir in skill_dirs:
        _compile_skill(skill_dir, out_root / skill_dir.name, overlay_name)
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


# ---------------------------------------------------------------------------
# opencode harness
# ---------------------------------------------------------------------------


def opencode_config_dir() -> Path:
    """opencode's user-scope config dir — ``$XDG_CONFIG_HOME/opencode`` or
    ``~/.config/opencode``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode"


def compile_opencode(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for opencode. opencode is pure path-discovery (no flags
    needed), so every scope is a bind:

    * **project instructions** — opencode reads the root ``./AGENTS.md`` natively
      (traversing up from the work dir), and that is exactly the agent-neutral source
      file, so agedum injects nothing for it (and could not — it is git-tracked);
    * **global instructions** — ``~/.config/agents/AGENTS.md`` (base merged with an
      optional ``AGENTS.opencode.md`` overlay) → ``<config>/AGENTS.md``, which opencode
      reads as its user-scope rules file;
    * **project skills** → ``./.opencode/skills/``; **global skills** →
      ``<config>/skills/``. opencode searches these *before* ``.agents/skills/`` /
      ``~/.agents/skills/``, so the overlaid copy (``SKILL.opencode.md`` merged in)
      wins over the raw source it would otherwise discover there.

    ``<config>`` is :func:`opencode_config_dir`. No ``extra_args`` — opencode discovers
    everything from disk.
    """
    plan = Plan()
    config = opencode_config_dir()

    # Project AGENTS.md is read natively from ./AGENTS.md (no bind) — record it so
    # --dry-run can show it rather than leave it invisible.
    if project.agents_md is not None:
        plan.native_reads.append(project.agents_md)

    # Global instructions (base + AGENTS.opencode.md overlay) -> <config>/AGENTS.md
    # (project ./AGENTS.md is read natively).
    global_instructions = _instructions(global_, "opencode") if global_ is not None else None
    if global_instructions is not None:
        out = dest / "global" / "AGENTS.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(global_instructions)
        target = config / "AGENTS.md"
        plan.binds.append((out, target))
        if global_ is not None and global_.agents_md is not None:
            plan.origins[target] = str(global_.agents_md)

    # Project skills -> ./.opencode/skills.
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills", "SKILL.opencode.md")
        if out is not None:
            target = project.root / ".opencode" / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(project.skills_dir)

    # Global skills -> <config>/skills.
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills", "SKILL.opencode.md")
        if out is not None:
            target = config / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(global_.skills_dir)

    return plan
