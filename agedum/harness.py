"""Compile the agent-neutral source into a harness's native on-disk layout.

Currently implements the Claude harness. Each scope is placed at *its own* Claude
location — they are never merged:

* **project** ``AGENTS.md`` → ``./CLAUDE.md``;
  ``.agents/skills/<n>/`` → ``./.claude/skills/<n>/``
* **global**  ``~/.config/agents/AGENTS.md`` → ``~/.claude/CLAUDE.md``;
  ``~/.config/agents/skills/<n>/`` → ``~/.claude/skills/<n>/``

Claude reads both scopes natively (user-scope `~/.claude/` + project-scope `./`), so
keeping them separate preserves the scope distinction. The compiled tree lives in a
throwaway directory; `Plan` records absolute (src → target) binds the launcher
mounts into the namespace.

Skills are discovered by walking ``.agents/skills/`` for every directory holding a
``SKILL.md`` (see :func:`_discover_skills`), so subfolders may group them — a nested
``group/skill/`` compiles to the flattened name ``group-skill``. Per skill: the base
``SKILL.md`` (minimal `name`/`description`) is merged with an optional ``SKILL.claude.md``
overlay; task files and scripts are copied verbatim; other harnesses' ``SKILL.<h>.md``
overlays are skipped.

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
import subprocess
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
    # Targets the launcher should tmpfs-shadow (mask with empty) rather than bind into.
    # Exempt from the git-tracked safety check (they are read-only shadows).
    safe_overrides: set[Path] = field(default_factory=set)
    # The harness's own state/config dir(s) — where it persists settings, sessions, auth, and
    # caches at run time. Under a sandbox launch these are mounted read-write (and created if
    # missing) so the harness can function, independent of what it happens to inject or what the
    # config's readWrite grants. Nothing is injected here — agedum only grants write access.
    # Ignored without a sandbox (the whole host is already read-write then).
    writable_dirs: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class Sandbox:
    """Filesystem confinement for the launched harness (write-confinement model).

    When ``enabled``, the launcher mounts the whole host **read-only** and makes
    writable only the project root, the nearest existing ancestor of each injected
    file (so bwrap can create the mount point), the harness's own state/config dir
    (``Plan.writable_dirs`` — so it can persist sessions/settings/auth, e.g.
    ``~/.cline`` or ``~/.claude``), every path in ``read_write``, and a private
    ``/tmp``. Everything else is read-only, so the agent cannot modify files outside
    its working set. When disabled (the default), the legacy full read-write host
    bind is used — the namespace then isolates only *what the harness reads as
    config*, not the filesystem.

    ``read_write`` holds raw path templates resolved at launch (see
    :func:`agedum.launcher.writable_roots`): ``~`` → the home dir, ``$VAR`` → the
    environment, ``${PROJECT_ROOT}`` → the project root.
    """

    enabled: bool = False
    read_write: tuple[str, ...] = ()


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
    """Claude settings ``hooks`` block that emits the session transcript.

    ``UserPromptSubmit`` frames the prompt as a ``[user]`` message; ``Stop`` tails
    the session JSONL after each assistant turn and frames the new assistant +
    thinking content. Both speak the neutral protocol condash decodes (the same one
    the opencode plugin emits), over two transports: the in-band OSC 7373 echo on
    ``/dev/tty`` and, when condash sets ``$CONDASH_TRANSCRIPT_FILE``, a per-tab
    sidecar file the hook appends to — reliable even when the hook runs without
    condash's controlling terminal. See ``assets/claude/emit-transcript.mjs``.
    """
    emitter = shlex.quote(_claude_emitter_path())
    return {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"node {emitter} user"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": f"node {emitter} stop"}]}],
    }


def _is_git_tracked(project_root: Path, target: Path) -> bool:
    """True when ``target`` is git-tracked in the repo at ``project_root``.

    Mirrors the launcher's safety check (:func:`agedum.launcher.assert_safe`) so the
    transcript injection can *skip* a tracked target instead of letting the launcher
    abort the whole run there. Returns False when ``project_root`` is not a repo or
    ``target`` is outside it.
    """
    try:
        rel = target.relative_to(project_root)
    except ValueError:
        return False
    if not (project_root / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--error-unmatch", str(rel)],
        capture_output=True,
    )
    return result.returncode == 0


def _inject_transcript_settings(plan: Plan, project_root: Path, proj_dest: Path) -> None:
    """Bind a project ``.claude/settings.local.json`` that adds transcript-capture hooks.

    Targets ``settings.local.json`` — Claude's highest-precedence, conventionally
    *untracked* local-overrides layer — NOT the often-versioned ``settings.json``: the
    launcher refuses to bind over a git-tracked path (the namespace shares the real
    ``.git``, so injected content could be committed), and many repos track
    ``settings.json`` deliberately. Hooks merge additively across layers, so ours still
    combine with the project's ``settings.json`` and the user's ``~/.claude`` hooks.
    If even ``settings.local.json`` is tracked, skip injection rather than break the
    launch — a captured transcript is never worth a failed launch.
    """
    target = project_root / ".claude" / "settings.local.json"
    if _is_git_tracked(project_root, target):
        return
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

    out = proj_dest / ".claude" / "settings.local.json"
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

    # Claude persists sessions/todos/statsig + OAuth cache under its config dir — writable
    # under a sandbox so the session works (see Plan.writable_dirs).
    plan.writable_dirs.append(claude_config_dir())

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
    # project .claude/settings.local.json that registers the transcript-capture hooks.
    # Default on, mirroring opencode; the OSC it emits is ignored by terminals that don't
    # decode it. Gated on an actual project source so a sourceless launch stays fully
    # transparent (no binds) and never injects into an unrelated directory.
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
        skills = _discover_skills(source.skills_dir)
        if skills:
            skills_out = dest / "skills"
            for name, skill_dir, nested in skills:
                _compile_skill(
                    skill_dir,
                    skills_out / name,
                    "SKILL.claude.md",
                    force_name=name if nested else None,
                )
            plan.binds.append((skills_out, skills_target))
            plan.origins[skills_target] = str(source.skills_dir)


def _compile_skill(src: Path, out: Path, overlay_name: str, force_name: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = (src / "SKILL.md").read_text() if (src / "SKILL.md").is_file() else ""
    overlay_path = src / overlay_name
    merged = base
    if overlay_path.is_file():
        merged = _merge_skill(base, overlay_path.read_text())
    # A nested skill's compiled identity is its flattened path (``group-skill``); force the
    # front-matter ``name`` to match so the harness invokes it by that name and two
    # like-named skills in different groups don't collide.
    if force_name is not None:
        merged = _set_skill_name(merged, force_name)
    (out / "SKILL.md").write_text(merged)

    # Copy task files / scripts / other assets; skip SKILL.md and any SKILL.<h>.md overlay.
    # A subdirectory that itself holds a SKILL.md is a separate (nested) skill, compiled on
    # its own — skip it here so it isn't also copied in as this skill's asset.
    for item in src.iterdir():
        if item.name == "SKILL.md" or (item.name.startswith("SKILL.") and item.suffix == ".md"):
            continue
        if item.is_dir() and any(item.rglob("SKILL.md")):
            continue
        dst = out / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)


def _discover_skills(skills_dir: Path) -> list[tuple[str, Path, bool]]:
    """Find every skill under ``skills_dir``, walking subdirectories.

    A skill is any directory containing a ``SKILL.md`` (at any depth). The compiled skill
    name is that directory's path relative to ``skills_dir`` with the components joined by
    ``-`` — so ``review/`` stays ``review`` and ``group/skill/`` becomes ``group-skill``,
    letting subfolders namespace skills. Returns ``(name, source_dir, nested)`` tuples in a
    deterministic order; ``nested`` is true when the skill sits below the top level (its
    front-matter ``name`` gets rewritten to the flattened identity). Raises ``ValueError``
    when two source directories flatten to the same name."""
    found: dict[str, Path] = {}
    skills: list[tuple[str, Path, bool]] = []
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        skill_src = skill_md.parent
        rel = skill_src.relative_to(skills_dir)
        name = "-".join(rel.parts)
        if name in found:
            raise ValueError(
                f"skill name collision: '{name}' from both "
                f"'{found[name].relative_to(skills_dir)}' and '{rel}'"
            )
        found[name] = skill_src
        skills.append((name, skill_src, len(rel.parts) > 1))
    return skills


def _set_skill_name(skill_md: str, name: str) -> str:
    """Return ``skill_md`` with its front-matter ``name`` set to ``name`` (used to give a
    nested skill the flattened identity that matches its compiled directory)."""
    meta, body = _split_frontmatter(skill_md)
    meta = {"name": name, **{k: v for k, v in meta.items() if k != "name"}}
    return _emit_frontmatter(meta, body)


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

    # kimi persists its state under ~/.kimi — writable under a sandbox (see Plan.writable_dirs).
    plan.writable_dirs.append(kimi_config_dir())

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
        # Bind the agent-file to a stable ~/.kimi path (like the skills binds) and pass that
        # to --agent-file, rather than the dest path directly: under write-confinement the
        # sandbox replaces /tmp with a private tmpfs, which would hide an agent-file referenced
        # at its dest (/tmp/agedum-…) path. The bind makes it visible regardless.
        target = kimi_config_dir() / "agedum-agent.yaml"
        plan.binds.append((agent_file, target))
        plan.extra_args += ["--agent-file", str(target)]
        if global_ is not None and global_.agents_md is not None:
            plan.origins[target] = str(global_.agents_md)

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
    """Compile each discovered skill under ``skills_dir`` into ``out_root/<name>/``, applying
    the ``overlay_name`` overlay; return ``out_root`` (or None when there are no skills).
    Skills nested in subfolders are flattened to ``group-skill`` names (see
    :func:`_discover_skills`)."""
    skills = _discover_skills(skills_dir)
    if not skills:
        return None
    for name, skill_dir, nested in skills:
        _compile_skill(
            skill_dir, out_root / name, overlay_name, force_name=name if nested else None
        )
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


def opencode_data_dir() -> Path:
    """opencode's user-scope data dir — ``$XDG_DATA_HOME/opencode`` or
    ``~/.local/share/opencode`` (auth, sessions, logs).

    Separate from :func:`opencode_config_dir`: opencode writes its runtime state (the login
    token, conversation history) here, so a sandbox launch must make it writable too."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
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
    * **project skills** → ``./.opencode/skills/``; **global skills**
      (``~/.config/agents/skills/``) → ``<config>/skills/``. opencode searches its config
      dir and ``./.opencode/skills/`` *before* the raw ``.agents/skills/`` /
      ``~/.agents/skills/`` it would otherwise discover, so the overlaid copy
      (``SKILL.opencode.md`` merged in) wins over any raw source.

    ``<config>`` is :func:`opencode_config_dir`. No ``extra_args`` — opencode discovers
    everything from disk.
    """
    plan = Plan()
    config = opencode_config_dir()

    # opencode writes config under <config> and runtime state (auth, sessions) under its data
    # dir — both writable under a sandbox (see Plan.writable_dirs).
    plan.writable_dirs += [config, opencode_data_dir()]

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


# ---------------------------------------------------------------------------
# Cline harness
# ---------------------------------------------------------------------------


def cline_config_dir() -> Path:
    """Cline's user-scope config dir — ``$CLINE_DATA_DIR`` or ``~/.cline``."""
    override = os.environ.get("CLINE_DATA_DIR")
    return Path(override) if override else Path.home() / ".cline"


def cline_global_agents_md() -> Path:
    """Cline's cross-tool *global* instructions path — ``~/.agents/AGENTS.md``.

    Cline reads a project-root ``AGENTS.md`` and the global ``~/.agents/AGENTS.md`` as
    cross-tool agent instructions. This is **not** under :func:`cline_config_dir`: Cline
    reads global *skills* from ``~/.cline/skills`` but global cross-tool *instructions*
    from ``~/.agents/AGENTS.md`` — the asymmetry is Cline's own.
    """
    return Path.home() / ".agents" / "AGENTS.md"


def compile_cline(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for Cline. Like opencode, Cline is pure path-discovery (no
    appended flags), so every scope is a bind or a native read:

    * **project instructions** — Cline reads the project-root ``./AGENTS.md`` natively
      (as a cross-tool rules file), and that is exactly the agent-neutral source file, so
      agedum injects nothing for it (and could not — it is git-tracked);
    * **global instructions** — ``~/.config/agents/AGENTS.md`` (base merged with an
      optional ``AGENTS.cline.md`` overlay) → ``~/.agents/AGENTS.md``, the cross-tool
      global path Cline reads (see :func:`cline_global_agents_md`);
    * **project skills** → ``./.cline/skills/``; **global skills** →
      ``<cline-config>/skills/``. Each skill is a ``SKILL.md`` folder, the same shape
      Cline expects; the ``SKILL.cline.md`` overlay is merged in.

    ``<cline-config>`` is :func:`cline_config_dir`. No ``extra_args`` — Cline discovers
    everything from disk.
    """
    plan = Plan()
    config = cline_config_dir()

    # Cline persists provider selection + task state under ~/.cline/data — writable under a
    # sandbox so it doesn't hit EROFS on ~/.cline/data/settings/providers.json (see
    # Plan.writable_dirs).
    plan.writable_dirs.append(config)

    # Project AGENTS.md is read natively from ./AGENTS.md (no bind) — record it so
    # --dry-run can show it rather than leave it invisible.
    if project.agents_md is not None:
        plan.native_reads.append(project.agents_md)

    # Global instructions (base + AGENTS.cline.md overlay) -> ~/.agents/AGENTS.md
    # (project ./AGENTS.md is read natively).
    global_instructions = _instructions(global_, "cline") if global_ is not None else None
    if global_instructions is not None:
        out = dest / "global" / "AGENTS.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(global_instructions)
        target = cline_global_agents_md()
        plan.binds.append((out, target))
        if global_ is not None and global_.agents_md is not None:
            plan.origins[target] = str(global_.agents_md)

    # Project skills -> ./.cline/skills.
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills", "SKILL.cline.md")
        if out is not None:
            target = project.root / ".cline" / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(project.skills_dir)

    # Global skills -> <cline-config>/skills.
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills", "SKILL.cline.md")
        if out is not None:
            target = config / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(global_.skills_dir)

    return plan


# ---------------------------------------------------------------------------
# reasonix harness (DeepSeek-Reasonix)
# ---------------------------------------------------------------------------


def reasonix_user_config_dir() -> Path:
    """reasonix's user-scope memory/config dir — ``$XDG_CONFIG_HOME/reasonix`` or
    ``~/.config/reasonix`` (reasonix's Go derives it from ``os.UserConfigDir()/reasonix``).

    reasonix loads its user-global memory doc (``REASONIX.md`` / ``AGENTS.md`` /
    ``CLAUDE.md``, in that order) from here at boot — see :func:`compile_reasonix`.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "reasonix"


def reasonix_home_skills_dir() -> Path:
    """reasonix's highest-priority *global* skills root — ``~/.reasonix/skills``.

    reasonix scans ``<home>/{.reasonix,.agents,.agent,.claude}/skills`` (and the same four
    under the project root) for skills, highest-priority first, with ``.reasonix`` leading.
    Injecting global skills here makes the overlaid copy win over the raw ``~/.agents/skills``
    reasonix also reads.
    """
    return Path.home() / ".reasonix" / "skills"


def compile_reasonix(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for reasonix (DeepSeek-Reasonix). Like opencode/cline, reasonix is
    pure path-discovery (no appended flags), so every scope is a bind or a native read:

    * **project instructions** — reasonix reads the project-root ``./AGENTS.md`` natively
      (one of its memory docs ``REASONIX.md`` / ``AGENTS.md`` / ``CLAUDE.md``, loaded at
      project scope and folded into the cache-stable system prompt), and that is exactly the
      agent-neutral source file, so agedum injects nothing for it (and could not — it is
      git-tracked);
    * **global instructions** — ``~/.config/agents/AGENTS.md`` (base merged with an optional
      ``AGENTS.reasonix.md`` overlay) → ``<userdir>/AGENTS.md`` where ``<userdir>`` is
      ``~/.config/reasonix`` (:func:`reasonix_user_config_dir`), the user-scope memory dir
      reasonix reads at boot;
    * **project skills** → ``./.reasonix/skills/``; **global skills** → ``~/.reasonix/skills/``
      (:func:`reasonix_home_skills_dir`). reasonix scans the four convention dirs
      (``.reasonix`` / ``.agents`` / ``.agent`` / ``.claude``, each ``/skills``) under both
      the project and home dirs, highest-priority first, and ``.reasonix`` leads — so the
      overlaid copy (``SKILL.reasonix.md`` merged in) injected there wins over the raw
      ``.agents/skills`` reasonix would also discover. Each skill is a ``SKILL.md`` folder,
      the shape reasonix expects.

    No ``extra_args`` — reasonix discovers everything from disk.
    """
    plan = Plan()

    # reasonix keeps its memory/config under ~/.config/reasonix and its home skills/state under
    # ~/.reasonix — both writable under a sandbox (see Plan.writable_dirs).
    plan.writable_dirs += [reasonix_user_config_dir(), Path.home() / ".reasonix"]

    # Project AGENTS.md is read natively from ./AGENTS.md (no bind) — record it so
    # --dry-run can show it rather than leave it invisible.
    if project.agents_md is not None:
        plan.native_reads.append(project.agents_md)

    # Global instructions (base + AGENTS.reasonix.md overlay) -> <userdir>/AGENTS.md
    # (project ./AGENTS.md is read natively).
    global_instructions = _instructions(global_, "reasonix") if global_ is not None else None
    if global_instructions is not None:
        out = dest / "global" / "AGENTS.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(global_instructions)
        target = reasonix_user_config_dir() / "AGENTS.md"
        plan.binds.append((out, target))
        if global_ is not None and global_.agents_md is not None:
            plan.origins[target] = str(global_.agents_md)

    # Project skills -> ./.reasonix/skills (highest-priority project skill root, so the
    # overlaid copy wins over the raw .agents/skills reasonix also reads natively).
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills", "SKILL.reasonix.md")
        if out is not None:
            target = project.root / ".reasonix" / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(project.skills_dir)

    # Global skills -> ~/.reasonix/skills (highest-priority global skill root).
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills", "SKILL.reasonix.md")
        if out is not None:
            target = reasonix_home_skills_dir()
            plan.binds.append((out, target))
            plan.origins[target] = str(global_.skills_dir)

    return plan


# ---------------------------------------------------------------------------
# aider harness
# ---------------------------------------------------------------------------


def compile_aider(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for aider.

    aider has **no native instruction discovery** (it reads neither ``AGENTS.md`` nor a
    ``CONVENTIONS.md`` on its own) and **no skills mechanism**. Its one channel for standing
    context is the ``--read`` flag, which adds a read-only file to the chat — the documented
    "conventions" mechanism. So agedum injects each scope's ``AGENTS.md`` as a ``--read``
    argument, the instructions analogue of kimi's ``--agent-file``:

    * **project instructions** — ``AGENTS.md`` → ``--read <compiled path>``;
    * **global instructions** — ``~/.config/agents/AGENTS.md`` (base merged with an optional
      ``AGENTS.aider.md`` overlay) → a second ``--read <compiled path>``.

    The compiled files live under the throwaway ``dest`` directory; the bwrap launch binds
    the whole real filesystem (``--dev-bind / /``), so those absolute paths resolve inside the
    namespace without a dedicated bind — ``Plan.binds`` stays empty (like kimi's agent file).

    **Skills are not injected.** aider has no skills system, so there is nothing to render
    them into (and no ``SKILL.aider.md`` overlay); a project ``.agents/skills/`` shows up in
    ``--dry-run`` as ``(not injected)``.
    """
    plan = Plan()

    # Project then global, each appended as its own --read. Project scope takes no overlay
    # (user scope only); global merges an optional AGENTS.aider.md.
    _aider_read(plan, _instructions(project, None), dest / "project", project.agents_md)
    if global_ is not None:
        _aider_read(plan, _instructions(global_, "aider"), dest / "global", global_.agents_md)

    return plan


def _aider_read(plan: Plan, instructions: str | None, dest: Path, source: Path | None) -> None:
    """Write one scope's ``AGENTS.md`` under ``dest`` and append it as an aider ``--read`` arg.

    A no-op when the scope has no ``AGENTS.md``. The compiled path is recorded in
    ``plan.origins`` so ``--dry-run`` can map the ``--read`` token back to its source.
    """
    if instructions is None:
        return
    out = dest / "AGENTS.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(instructions)
    plan.extra_args += ["--read", str(out)]
    if source is not None:
        plan.origins[out] = str(source)


# ---------------------------------------------------------------------------
# pi harness (earendil-works pi-coding-agent)
# ---------------------------------------------------------------------------


def pi_agent_dir() -> Path:
    """pi's user-scope agent dir — ``$PI_CODING_AGENT_DIR`` or ``~/.pi/agent``.

    pi reads its user-global context file (``AGENTS.md`` / ``CLAUDE.md``), skills,
    ``models.json``, and ``settings.json`` from here (pi's ``getAgentDir()``).
    """
    override = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(override) if override else Path.home() / ".pi" / "agent"


def compile_pi(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for pi (earendil-works pi-coding-agent). Like opencode/cline/
    reasonix, pi is pure path-discovery (no appended flags), so every scope is a bind or a
    native read:

    * **project instructions** — pi walks cwd→root collecting ``AGENTS.md``/``CLAUDE.md``
      (``loadProjectContextFiles``), so the project-root ``./AGENTS.md`` is read **natively**
      — that is exactly the agent-neutral source file, so agedum injects nothing for it (and
      could not — it is git-tracked);
    * **global instructions** — ``~/.config/agents/AGENTS.md`` (base merged with an optional
      ``AGENTS.pi.md`` overlay) → ``<agentdir>/AGENTS.md`` where ``<agentdir>`` is
      ``~/.pi/agent`` (:func:`pi_agent_dir`), the highest-priority user-scope context file pi
      reads (``loadContextFileFromDir(agentDir)``, with ``AGENTS.md`` the first candidate);
    * **project skills** → ``./.pi/skills/``; **global skills** → ``~/.pi/agent/skills/``.
      pi auto-discovers ``SKILL.md`` folders from both dirs; each skill carries
      ``name``/``description`` frontmatter — the neutral source shape — with a ``SKILL.pi.md``
      overlay merged in.

    No ``extra_args`` — pi discovers everything from disk.
    """
    plan = Plan()
    agent_dir = pi_agent_dir()

    # pi persists settings/models/session state under its agent dir — writable under a sandbox
    # (see Plan.writable_dirs).
    plan.writable_dirs.append(agent_dir)

    # Project AGENTS.md is read natively from ./AGENTS.md (no bind) — record it so
    # --dry-run can show it rather than leave it invisible.
    if project.agents_md is not None:
        plan.native_reads.append(project.agents_md)

    # Global instructions (base + AGENTS.pi.md overlay) -> <agentdir>/AGENTS.md
    # (project ./AGENTS.md is read natively).
    global_instructions = _instructions(global_, "pi") if global_ is not None else None
    if global_instructions is not None:
        out = dest / "global" / "AGENTS.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(global_instructions)
        target = agent_dir / "AGENTS.md"
        plan.binds.append((out, target))
        if global_ is not None and global_.agents_md is not None:
            plan.origins[target] = str(global_.agents_md)

    # Project skills -> ./.pi/skills.
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills", "SKILL.pi.md")
        if out is not None:
            target = project.root / ".pi" / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(project.skills_dir)
        # Shadow .agents/skills/ with an empty tmpfs so pi does not discover the raw
        # agent-neutral source skills alongside the compiled .pi/skills/ copies — that
        # would produce a name-collision warning for every shared skill. Only when the
        # source dir exists — shadowing a missing path would just make bwrap stub it.
        plan.safe_overrides.add(project.root / ".agents" / "skills")

    # Global skills -> ~/.pi/agent/skills.
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills", "SKILL.pi.md")
        if out is not None:
            target = agent_dir / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(global_.skills_dir)

    return plan


# ---------------------------------------------------------------------------
# codex harness (OpenAI Codex CLI)
# ---------------------------------------------------------------------------


def codex_config_dir() -> Path:
    """codex's home/state dir — ``$CODEX_HOME`` or ``~/.codex``.

    codex stores all local state here (``config.toml``, ``auth.json``, sessions, logs)
    and reads its user-scope ``AGENTS.md``, ``skills/``, and custom-agent ``agents/``
    from it (codex's ``CODEX_HOME``, default ``~/.codex``).
    """
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def compile_codex(project: Source, global_: Source | None, dest: Path) -> Plan:
    """Render the source for codex (OpenAI Codex CLI). Like opencode/pi, codex is pure
    path-discovery (no appended flags), so every scope is a bind or a native read:

    * **project instructions** — codex walks the work dir up to the project root collecting
      ``AGENTS.md`` and folds it into the first turn, so the project-root ``./AGENTS.md`` is
      read **natively** — that is exactly the agent-neutral source file, so agedum injects
      nothing for it (and could not — it is git-tracked);
    * **global instructions** — ``~/.config/agents/AGENTS.md`` (base merged with an optional
      ``AGENTS.codex.md`` overlay) → ``<codex-home>/AGENTS.md`` where ``<codex-home>`` is
      ``~/.codex`` (:func:`codex_config_dir`), the user-scope rules file codex reads at boot
      (``$CODEX_HOME/AGENTS.md``; an ``AGENTS.override.md`` would take precedence, but agedum
      writes the base name);
    * **project skills** → ``./.codex/skills/``; **global skills** → ``<codex-home>/skills/``.
      codex auto-discovers ``SKILL.md`` folders from its home ``skills/`` and the project
      ``.codex/skills/``; each skill carries ``name``/``description`` frontmatter — the neutral
      source shape — with a ``SKILL.codex.md`` overlay merged in.

    No ``extra_args`` — codex discovers everything from disk; the provider's model + custom
    endpoint ride ``-c``/``-m`` flags built in :func:`agedum.provider._codex_env`.
    """
    plan = Plan()
    config = codex_config_dir()

    # codex stores all local state (config.toml, auth.json, sessions, logs) under its home dir —
    # writable under a sandbox (see Plan.writable_dirs).
    plan.writable_dirs.append(config)

    # Project AGENTS.md is read natively from ./AGENTS.md (no bind) — record it so
    # --dry-run can show it rather than leave it invisible.
    if project.agents_md is not None:
        plan.native_reads.append(project.agents_md)

    # Global instructions (base + AGENTS.codex.md overlay) -> <codex-home>/AGENTS.md
    # (project ./AGENTS.md is read natively).
    global_instructions = _instructions(global_, "codex") if global_ is not None else None
    if global_instructions is not None:
        out = dest / "global" / "AGENTS.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(global_instructions)
        target = config / "AGENTS.md"
        plan.binds.append((out, target))
        if global_ is not None and global_.agents_md is not None:
            plan.origins[target] = str(global_.agents_md)

    # Project skills -> ./.codex/skills.
    if project.skills_dir is not None:
        out = _compile_skill_tree(project.skills_dir, dest / "project-skills", "SKILL.codex.md")
        if out is not None:
            target = project.root / ".codex" / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(project.skills_dir)

    # Global skills -> <codex-home>/skills.
    if global_ is not None and global_.skills_dir is not None:
        out = _compile_skill_tree(global_.skills_dir, dest / "global-skills", "SKILL.codex.md")
        if out is not None:
            target = config / "skills"
            plan.binds.append((out, target))
            plan.origins[target] = str(global_.skills_dir)

    return plan
