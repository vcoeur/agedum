import json
import subprocess
from pathlib import Path

import pytest

from agedum.harness import (
    Plan,
    _discover_skills,
    claude_config_dir,
    cline_config_dir,
    cline_global_agents_md,
    codex_config_dir,
    compile_aider,
    compile_claude,
    compile_cline,
    compile_codex,
    compile_kimi,
    compile_opencode,
    compile_pi,
    compile_reasonix,
    kimi_config_dir,
    opencode_config_dir,
    opencode_data_dir,
    pi_agent_dir,
    reasonix_home_skills_dir,
    reasonix_user_config_dir,
)
from agedum.launcher import assert_safe, build_bwrap_argv
from agedum.sources import Source, load_source


def _targets(plan):
    return [t for _, t in plan.binds]


def _src_for(plan, target):
    for src, t in plan.binds:
        if t == target:
            return src
    raise AssertionError(f"no bind for target {target}")


def test_compile_claude_project_layout_and_overlay(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# project instructions\n")
    skill = tmp_path / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: a demo\n---\nBase body.\n")
    (skill / "SKILL.claude.md").write_text("---\nallowed-tools: Read, Bash\n---\nClaude note.\n")
    (skill / "task1.md").write_text("task one\n")
    (skill / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    (skill / "SKILL.kimi.md").write_text("---\nx: 1\n---\nkimi\n")

    src = load_source(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)

    # Project scope binds to in-tree, absolute Claude paths.
    assert _src_for(plan, tmp_path / "CLAUDE.md").read_text() == "# project instructions\n"
    skills_src = _src_for(plan, tmp_path / ".claude" / "skills")

    # Provenance (for --dry-run): each target maps back to its agent-neutral source.
    assert plan.origins[tmp_path / "CLAUDE.md"] == str(tmp_path / "AGENTS.md")
    assert plan.origins[tmp_path / ".claude" / "skills"] == str(tmp_path / ".agents" / "skills")

    skill_md = (skills_src / "demo" / "SKILL.md").read_text()
    assert "name: demo" in skill_md
    assert "description: a demo" in skill_md
    assert "allowed-tools:" in skill_md  # claude overlay frontmatter merged in
    assert "Base body." in skill_md and "Claude note." in skill_md

    assert (skills_src / "demo" / "task1.md").exists()
    assert (skills_src / "demo" / "helper.sh").exists()
    assert not (skills_src / "demo" / "SKILL.kimi.md").exists()


def test_discover_skills_walks_subfolders_and_flattens_names(tmp_path):
    root = tmp_path / ".agents" / "skills"
    (root / "review").mkdir(parents=True)
    (root / "review" / "SKILL.md").write_text("---\nname: review\ndescription: d\n---\n")
    (root / "git" / "commit").mkdir(parents=True)
    (root / "git" / "commit" / "SKILL.md").write_text("---\nname: commit\ndescription: d\n---\n")
    (root / "git" / "pr").mkdir(parents=True)
    (root / "git" / "pr" / "SKILL.md").write_text("---\nname: pr\ndescription: d\n---\n")

    discovered = _discover_skills(root)
    assert discovered == [
        ("git-commit", root / "git" / "commit", True),
        ("git-pr", root / "git" / "pr", True),
        ("review", root / "review", False),
    ]


def test_discover_skills_raises_on_flattened_name_collision(tmp_path):
    root = tmp_path / ".agents" / "skills"
    (root / "group-skill").mkdir(parents=True)
    (root / "group-skill" / "SKILL.md").write_text("---\nname: group-skill\ndescription: d\n---\n")
    (root / "group" / "skill").mkdir(parents=True)
    (root / "group" / "skill" / "SKILL.md").write_text("---\nname: skill\ndescription: d\n---\n")

    with pytest.raises(ValueError, match="skill name collision: 'group-skill'"):
        _discover_skills(root)


def test_compile_claude_nested_skill_flattens_name_and_dir(tmp_path):
    # A skill in a grouping subfolder is compiled as `group-skill`, and its front-matter
    # `name` is rewritten to match; a top-level skill keeps its declared name.
    (tmp_path / "AGENTS.md").write_text("# p\n")
    top = tmp_path / ".agents" / "skills" / "demo"
    top.mkdir(parents=True)
    (top / "SKILL.md").write_text("---\nname: demo\ndescription: top\n---\nBody.\n")
    nested = tmp_path / ".agents" / "skills" / "git" / "commit"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: commit\ndescription: nested\n---\nCommit body.\n")
    (nested / "helper.sh").write_text("#!/bin/sh\necho hi\n")

    plan = compile_claude(load_source(tmp_path), None, tmp_path / "out")
    skills_src = _src_for(plan, tmp_path / ".claude" / "skills")

    # Top-level skill: unchanged identity.
    assert (skills_src / "demo" / "SKILL.md").read_text().count("name: demo") == 1

    # Nested skill: compiled at the flattened dir with a rewritten `name`, description kept,
    # body preserved, and its asset carried through.
    nested_md = (skills_src / "git-commit" / "SKILL.md").read_text()
    assert "name: git-commit" in nested_md
    assert "name: commit" not in nested_md
    assert "description: nested" in nested_md
    assert "Commit body." in nested_md
    assert (skills_src / "git-commit" / "helper.sh").exists()
    # No stray `git/` grouping dir leaks into the compiled tree.
    assert not (skills_src / "git").exists()


def test_compile_claude_skill_nested_inside_skill_not_double_copied(tmp_path):
    # A skill that itself contains a nested skill: the child is compiled on its own and is
    # NOT also copied in as the parent's asset.
    (tmp_path / "AGENTS.md").write_text("# p\n")
    parent = tmp_path / ".agents" / "skills" / "outer"
    parent.mkdir(parents=True)
    (parent / "SKILL.md").write_text("---\nname: outer\ndescription: parent\n---\n")
    (parent / "notes.md").write_text("asset\n")
    child = parent / "inner"
    child.mkdir()
    (child / "SKILL.md").write_text("---\nname: inner\ndescription: child\n---\n")

    plan = compile_claude(load_source(tmp_path), None, tmp_path / "out")
    skills_src = _src_for(plan, tmp_path / ".claude" / "skills")

    # Both skills exist, at their own compiled dirs.
    assert (skills_src / "outer" / "SKILL.md").exists()
    assert (skills_src / "outer-inner" / "SKILL.md").exists()
    # The child subtree is not duplicated inside the parent; plain assets still copy.
    assert not (skills_src / "outer" / "inner").exists()
    assert (skills_src / "outer" / "notes.md").exists()


def test_compile_kimi_discovers_nested_skills(tmp_path, monkeypatch):
    # The shared _compile_skill_tree path (kimi/opencode/cline/…) also walks subfolders.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "AGENTS.md").write_text("# p\n")
    nested = tmp_path / ".agents" / "skills" / "group" / "skill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: skill\ndescription: d\n---\nBody.\n")

    plan = compile_kimi(load_source(tmp_path), None, tmp_path / "out")
    skills_src = _src_for(plan, tmp_path / ".kimi-code" / "skills")
    nested_md = (skills_src / "group-skill" / "SKILL.md").read_text()
    assert "name: group-skill" in nested_md


def test_load_source_excludes_home_as_project_root(tmp_path, monkeypatch):
    # $HOME can match find_project_root — it may hold ~/.agents/ (cline's global AGENTS.md
    # sink, or a legacy pre-XDG ~/.agents/skills) — but home is not a project. load_source
    # must yield an empty project source there, or home-level files get re-injected as
    # project scope.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".agents" / "skills" / "demo").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("# would-be project instructions\n")

    src = load_source(tmp_path)
    assert src.skills_dir is None
    assert src.agents_md is None


def test_compile_with_no_source_is_empty(tmp_path):
    (tmp_path / ".git").mkdir()
    src = load_source(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)
    assert plan.binds == []


def test_global_scope_lands_at_claude_config_dir_separately(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".agents" / "skills" / "pskill").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTRUCTIONS\n")
    (proj / ".agents" / "skills" / "pskill" / "SKILL.md").write_text(
        "---\nname: pskill\ndescription: d\n---\n"
    )

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTRUCTIONS\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    cc = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    assert claude_config_dir() == cc

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(project, global_, dest)

    targets = _targets(plan)
    # project scope -> in-tree; global scope -> the user-scope Claude dir, SEPARATELY
    assert proj / "CLAUDE.md" in targets
    assert proj / ".claude" / "skills" in targets
    assert cc / "CLAUDE.md" in targets
    assert cc / "skills" in targets

    # NOT concatenated — each scope's CLAUDE.md holds only its own content
    assert _src_for(plan, proj / "CLAUDE.md").read_text() == "PROJECT-INSTRUCTIONS\n"
    assert _src_for(plan, cc / "CLAUDE.md").read_text() == "GLOBAL-INSTRUCTIONS\n"
    assert (_src_for(plan, cc / "skills") / "gskill" / "SKILL.md").exists()
    assert (_src_for(plan, proj / ".claude" / "skills") / "pskill" / "SKILL.md").exists()


def test_compile_kimi(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")
    (sk / "SKILL.kimi.md").write_text("---\nkimi_only: 1\n---\nkimi note\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    home = tmp_path / "home"
    (home / ".kimi-code").mkdir(parents=True)
    (home / ".kimi-code" / "config.toml").write_text('default_model = "x"\n')
    monkeypatch.setenv("HOME", str(home))
    assert kimi_config_dir() == home / ".kimi-code"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_kimi(project, global_, dest)

    # Global instructions -> bound at ~/.kimi-code/AGENTS.md (the user-scope AGENTS.md Kimi
    # reads natively). The project AGENTS.md is read natively (./AGENTS.md), so it is NOT
    # injected here — and no flag is appended.
    assert plan.extra_args == []
    agents_target = home / ".kimi-code" / "AGENTS.md"
    assert agents_target in [t for _, t in plan.binds]
    agents_text = _src_for(plan, agents_target).read_text()
    assert "GLOBAL-INSTR" in agents_text
    assert "PROJECT-INSTR" not in agents_text

    # The natively-read project AGENTS.md is recorded so --dry-run can surface it.
    assert (proj / "AGENTS.md") in plan.native_reads

    # Global skills -> bound into ~/.kimi-code/skills.
    assert (home / ".kimi-code" / "skills") in [t for _, t in plan.binds]
    assert (_src_for(plan, home / ".kimi-code" / "skills") / "gskill" / "SKILL.md").exists()

    # Project skills -> ./.kimi-code/skills (project-local bind), kimi overlay applied.
    assert (proj / ".kimi-code" / "skills") in [t for _, t in plan.binds]
    pskill_dir = _src_for(plan, proj / ".kimi-code" / "skills")
    pskill_md = (pskill_dir / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "kimi note" in pskill_md


def test_compile_kimi_project_only_injects_nothing(tmp_path):
    # A project with its own AGENTS.md but no global scope: kimi reads ./AGENTS.md
    # natively, so there is nothing to inject — no bind, no appended flag.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_kimi(project, None, dest)

    assert plan.extra_args == []
    assert plan.binds == []


def test_compile_opencode(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")
    (sk / "SKILL.opencode.md").write_text("---\nlicense: MIT\n---\nopencode note\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    config = opencode_config_dir()
    assert config == xdg / "opencode"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_opencode(project, global_, dest)

    targets = _targets(plan)
    # opencode is pure path-discovery — no extra args.
    assert plan.extra_args == []

    # Project AGENTS.md is read natively at ./AGENTS.md — never injected, but recorded
    # as a native read so --dry-run can surface it.
    assert proj / "AGENTS.md" not in targets
    assert proj / "AGENTS.md" in plan.native_reads

    # Global instructions -> <config>/AGENTS.md.
    assert config / "AGENTS.md" in targets
    assert _src_for(plan, config / "AGENTS.md").read_text() == "GLOBAL-INSTR\n"

    # Project skills -> ./.opencode/skills, with the opencode overlay merged.
    assert proj / ".opencode" / "skills" in targets
    pskill_md = (_src_for(plan, proj / ".opencode" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "license: MIT" in pskill_md
    assert "opencode note" in pskill_md

    # Global skills -> <config>/skills.
    assert config / "skills" in targets
    assert (_src_for(plan, config / "skills") / "gskill" / "SKILL.md").exists()


def test_compile_opencode_project_only_injects_skills_not_instructions(tmp_path):
    # No global scope: project AGENTS.md is native (no bind), only project skills bind.
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_opencode(project, None, dest)

    assert plan.extra_args == []
    assert _targets(plan) == [proj / ".opencode" / "skills"]


def test_compile_claude_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    # Global AGENTS.md + AGENTS.claude.md sibling -> merged into ~/.claude/CLAUDE.md;
    # an AGENTS.kimi.md sibling is ignored when compiling for Claude.
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.claude.md").write_text("CLAUDE-EXTRA\n")
    (gconf / "AGENTS.kimi.md").write_text("KIMI-EXTRA\n")

    cc = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(load_source(tmp_path / "noproj"), global_, dest)

    merged = _src_for(plan, cc / "CLAUDE.md").read_text()
    assert "GLOBAL-BASE" in merged
    assert "CLAUDE-EXTRA" in merged
    assert "KIMI-EXTRA" not in merged  # wrong-harness overlay ignored


def test_compile_claude_project_agents_harness_overlay_not_merged(tmp_path):
    # The overlay applies at user scope only: a project-scope AGENTS.claude.md is NOT
    # merged into ./CLAUDE.md.
    (tmp_path / "AGENTS.md").write_text("PROJECT-BASE\n")
    (tmp_path / "AGENTS.claude.md").write_text("PROJECT-CLAUDE-EXTRA\n")

    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(load_source(tmp_path), None, dest)

    assert _src_for(plan, tmp_path / "CLAUDE.md").read_text() == "PROJECT-BASE\n"


def test_compile_kimi_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.kimi.md").write_text("KIMI-EXTRA\n")
    (gconf / "AGENTS.claude.md").write_text("CLAUDE-EXTRA\n")

    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_kimi(load_source(tmp_path / "noproj"), global_, dest)

    agents_target = kimi_config_dir() / "AGENTS.md"
    agents_text = _src_for(plan, agents_target).read_text()
    assert "GLOBAL-BASE" in agents_text
    assert "KIMI-EXTRA" in agents_text
    assert "CLAUDE-EXTRA" not in agents_text  # wrong-harness overlay ignored


def test_compile_opencode_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.opencode.md").write_text("OPENCODE-EXTRA\n")
    (gconf / "AGENTS.kimi.md").write_text("KIMI-EXTRA\n")

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    config = opencode_config_dir()

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_opencode(load_source(tmp_path / "noproj"), global_, dest)

    merged = _src_for(plan, config / "AGENTS.md").read_text()
    assert "GLOBAL-BASE" in merged
    assert "OPENCODE-EXTRA" in merged
    assert "KIMI-EXTRA" not in merged  # wrong-harness overlay ignored


def test_compile_cline(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")
    (sk / "SKILL.cline.md").write_text("---\nlicense: MIT\n---\ncline note\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data_dir = tmp_path / "cline-data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    assert cline_config_dir() == data_dir
    assert cline_global_agents_md() == home / ".agents" / "AGENTS.md"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_cline(project, global_, dest)

    targets = _targets(plan)
    # Cline is pure path-discovery — no extra args.
    assert plan.extra_args == []

    # Project AGENTS.md is read natively at ./AGENTS.md — never injected, but recorded.
    assert proj / "AGENTS.md" not in targets
    assert proj / "AGENTS.md" in plan.native_reads

    # Global instructions -> the cross-tool ~/.agents/AGENTS.md (NOT under CLINE_DATA_DIR).
    assert home / ".agents" / "AGENTS.md" in targets
    assert _src_for(plan, home / ".agents" / "AGENTS.md").read_text() == "GLOBAL-INSTR\n"

    # Project skills -> ./.cline/skills, with the cline overlay merged.
    assert proj / ".cline" / "skills" in targets
    pskill_md = (_src_for(plan, proj / ".cline" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "license: MIT" in pskill_md
    assert "cline note" in pskill_md

    # Global skills -> <CLINE_DATA_DIR>/skills.
    assert data_dir / "skills" in targets
    assert (_src_for(plan, data_dir / "skills") / "gskill" / "SKILL.md").exists()

    # Cline's state dir is granted write access under a sandbox — the fix for the EROFS on
    # ~/.cline/data/settings/providers.json.
    assert data_dir in plan.writable_dirs


def test_compile_cline_project_only_injects_skills_not_instructions(tmp_path):
    # No global scope: project AGENTS.md is native (no bind), only project skills bind.
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_cline(project, None, dest)

    assert plan.extra_args == []
    assert _targets(plan) == [proj / ".cline" / "skills"]


def test_compile_cline_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.cline.md").write_text("CLINE-EXTRA\n")
    (gconf / "AGENTS.opencode.md").write_text("OPENCODE-EXTRA\n")

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_cline(load_source(tmp_path / "noproj"), global_, dest)

    merged = _src_for(plan, cline_global_agents_md()).read_text()
    assert "GLOBAL-BASE" in merged
    assert "CLINE-EXTRA" in merged
    assert "OPENCODE-EXTRA" not in merged  # wrong-harness overlay ignored


def test_compile_declares_harness_state_dirs(tmp_path, monkeypatch):
    # Each harness declares its own state/config dir in Plan.writable_dirs so a sandbox launch
    # can persist runtime state (sessions/settings/auth) — independent of what it injects.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "CLAUDE_CONFIG_DIR",
        "CLINE_DATA_DIR",
        "CODEX_HOME",
        "PI_CODING_AGENT_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        monkeypatch.delenv(var, raising=False)

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    project = load_source(proj)

    expected = {
        compile_claude: [claude_config_dir()],
        compile_kimi: [kimi_config_dir()],
        compile_opencode: [opencode_config_dir(), opencode_data_dir()],
        compile_cline: [cline_config_dir()],
        compile_reasonix: [reasonix_user_config_dir(), home / ".reasonix"],
        compile_pi: [pi_agent_dir()],
        compile_codex: [codex_config_dir()],
    }
    for index, (compile_fn, dirs) in enumerate(expected.items()):
        dest = tmp_path / f"out-{index}"
        dest.mkdir()
        plan = compile_fn(project, None, dest)
        for path in dirs:
            assert path in plan.writable_dirs, f"{compile_fn.__name__} missing {path}"

    # aider has no home-scope state dir — it writes its state in-cwd (the project root, always
    # writable), so it declares none.
    aider_dest = tmp_path / "out-aider"
    aider_dest.mkdir()
    assert compile_aider(project, None, aider_dest).writable_dirs == []


def test_compile_reasonix(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")
    (sk / "SKILL.reasonix.md").write_text("---\nlicense: MIT\n---\nreasonix note\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert reasonix_user_config_dir() == xdg / "reasonix"
    assert reasonix_home_skills_dir() == home / ".reasonix" / "skills"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_reasonix(project, global_, dest)

    targets = _targets(plan)
    # reasonix is pure path-discovery — no extra args.
    assert plan.extra_args == []

    # Project AGENTS.md is read natively at ./AGENTS.md — never injected, but recorded.
    assert proj / "AGENTS.md" not in targets
    assert proj / "AGENTS.md" in plan.native_reads

    # Global instructions -> <userdir>/AGENTS.md (~/.config/reasonix, the user memory dir).
    assert xdg / "reasonix" / "AGENTS.md" in targets
    assert _src_for(plan, xdg / "reasonix" / "AGENTS.md").read_text() == "GLOBAL-INSTR\n"

    # Project skills -> ./.reasonix/skills (highest-priority root), overlay merged.
    assert proj / ".reasonix" / "skills" in targets
    pskill_md = (_src_for(plan, proj / ".reasonix" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "license: MIT" in pskill_md
    assert "reasonix note" in pskill_md

    # Global skills -> ~/.reasonix/skills.
    assert home / ".reasonix" / "skills" in targets
    assert (_src_for(plan, home / ".reasonix" / "skills") / "gskill" / "SKILL.md").exists()


def test_compile_reasonix_project_only_injects_skills_not_instructions(tmp_path):
    # No global scope: project AGENTS.md is native (no bind), only project skills bind.
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_reasonix(project, None, dest)

    assert plan.extra_args == []
    assert _targets(plan) == [proj / ".reasonix" / "skills"]


def test_compile_reasonix_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.reasonix.md").write_text("REASONIX-EXTRA\n")
    (gconf / "AGENTS.opencode.md").write_text("OPENCODE-EXTRA\n")

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_reasonix(load_source(tmp_path / "noproj"), global_, dest)

    merged = _src_for(plan, reasonix_user_config_dir() / "AGENTS.md").read_text()
    assert "GLOBAL-BASE" in merged
    assert "REASONIX-EXTRA" in merged
    assert "OPENCODE-EXTRA" not in merged  # wrong-harness overlay ignored


def _git_init_commit(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )


def test_compile_claude_injects_transcript_hooks_into_settings_local(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# proj\n")
    src = load_source(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)

    targets = [t for _, t in plan.binds]
    # Hooks land in settings.local.json (untracked layer), never settings.json.
    assert tmp_path / ".claude" / "settings.local.json" in targets
    assert tmp_path / ".claude" / "settings.json" not in targets

    settings = json.loads(_src_for(plan, tmp_path / ".claude" / "settings.local.json").read_text())
    events = settings["hooks"]
    assert set(events) == {"UserPromptSubmit", "Stop"}
    user_cmd = events["UserPromptSubmit"][0]["hooks"][0]["command"]
    stop_cmd = events["Stop"][0]["hooks"][0]["command"]
    assert user_cmd.startswith("node ")
    assert user_cmd.endswith("emit-transcript.mjs user")
    assert stop_cmd.endswith("emit-transcript.mjs stop")


def test_compile_claude_transcript_hooks_merge_existing_local_settings(tmp_path):
    # An existing settings.local.json keeps its keys + hooks; transcript hooks add alongside.
    (tmp_path / "AGENTS.md").write_text("# proj\n")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read(//x/**)"]},
                "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
            }
        )
    )

    src = load_source(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)

    merged = json.loads(_src_for(plan, tmp_path / ".claude" / "settings.local.json").read_text())
    assert merged["permissions"] == {"allow": ["Read(//x/**)"]}
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert "UserPromptSubmit" in merged["hooks"]
    assert "Stop" in merged["hooks"]


def test_compile_claude_launch_safe_when_settings_json_tracked(tmp_path):
    # Regression: a repo that git-TRACKS .claude/settings.json must still launch. Hooks go
    # into the untracked settings.local.json; the tracked file is never bound, so the
    # launcher's assert_safe does not abort.
    from agedum.launcher import assert_safe

    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("# proj\n")
    (proj / ".claude" / "settings.json").write_text('{"permissions": {"allow": []}}\n')
    (proj / ".gitignore").write_text("CLAUDE.md\n.claude/settings.local.json\n")
    _git_init_commit(proj)  # tracks settings.json; settings.local.json is gitignored

    src = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)  # must not raise

    targets = [t for _, t in plan.binds]
    assert proj / ".claude" / "settings.local.json" in targets
    assert proj / ".claude" / "settings.json" not in targets
    assert_safe(proj, plan)  # the launch-time safety check must pass


def test_compile_claude_skips_hooks_when_settings_local_tracked(tmp_path):
    # If even settings.local.json is tracked, skip injection (graceful) — never abort.
    # NB: a global gitignore (`**/.claude/*.local.*`) may exclude it from `add -A`, so
    # force-add to genuinely track it and exercise the guard.
    from agedum.launcher import assert_safe

    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("# proj\n")
    (proj / ".claude" / "settings.local.json").write_text("{}\n")
    (proj / ".gitignore").write_text("CLAUDE.md\n")
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    subprocess.run(["git", "-C", str(proj), "add", "-f", ".claude/settings.local.json"], check=True)
    subprocess.run(["git", "-C", str(proj), "add", "AGENTS.md", ".gitignore"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(proj),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )

    src = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)

    targets = [t for _, t in plan.binds]
    assert proj / ".claude" / "settings.local.json" not in targets  # skipped, not bound
    assert_safe(proj, plan)  # must not raise


def test_claude_emitter_asset_is_shipped():
    from agedum.harness import _claude_emitter_path

    assert Path(_claude_emitter_path()).is_file()


def _aider_reads(plan):
    """Paths following each ``--read`` in the plan's extra_args."""
    return [plan.extra_args[i + 1] for i, token in enumerate(plan.extra_args) if token == "--read"]


def test_compile_aider(tmp_path):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_aider(project, global_, dest)

    # aider has no native instruction discovery — both scopes' AGENTS.md ride a --read
    # (project first, then global), since aider reads no AGENTS.md natively.
    reads = _aider_reads(plan)
    assert len(reads) == 2
    assert Path(reads[0]).read_text() == "PROJECT-INSTR\n"
    assert Path(reads[1]).read_text() == "GLOBAL-INSTR\n"

    # No binds: the compiled files are read at their real dest path via --dev-bind / /.
    assert plan.binds == []

    # Skills are NOT injected — aider has no skills mechanism, so neither scope's skills
    # appear anywhere in the plan.
    assert all("skills" not in token for token in plan.extra_args)

    # Provenance for --dry-run: each --read path maps back to its AGENTS.md source.
    assert plan.origins[Path(reads[0])] == str(proj / "AGENTS.md")
    assert plan.origins[Path(reads[1])] == str(gconf / "AGENTS.md")


def test_compile_aider_project_only(tmp_path):
    # A project with its own AGENTS.md but no global scope: one --read, no binds.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_aider(project, None, dest)

    reads = _aider_reads(plan)
    assert len(reads) == 1
    assert Path(reads[0]).read_text() == "PROJECT-INSTR\n"
    assert plan.binds == []


def test_compile_aider_skills_only_is_empty(tmp_path):
    # aider has no skills mechanism, so a project with only skills (no AGENTS.md) yields
    # nothing to inject — no binds and no --read.
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_aider(project, None, dest)

    assert plan.binds == []
    assert plan.extra_args == []


def test_compile_aider_global_agents_harness_overlay_merged(tmp_path):
    # Global AGENTS.md + AGENTS.aider.md sibling -> merged into the global --read; an
    # AGENTS.opencode.md sibling is ignored when compiling for aider.
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.aider.md").write_text("AIDER-EXTRA\n")
    (gconf / "AGENTS.opencode.md").write_text("OPENCODE-EXTRA\n")

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_aider(load_source(tmp_path / "noproj"), global_, dest)

    reads = _aider_reads(plan)
    assert len(reads) == 1
    merged = Path(reads[0]).read_text()
    assert "GLOBAL-BASE" in merged
    assert "AIDER-EXTRA" in merged
    assert "OPENCODE-EXTRA" not in merged  # wrong-harness overlay ignored


def test_compile_pi(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")
    (sk / "SKILL.pi.md").write_text("---\nlicense: MIT\n---\npi note\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    assert pi_agent_dir() == home / ".pi" / "agent"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_pi(project, global_, dest)

    targets = _targets(plan)
    # pi is pure path-discovery — no extra args.
    assert plan.extra_args == []

    # Project AGENTS.md is read natively at ./AGENTS.md — never injected, but recorded.
    assert proj / "AGENTS.md" not in targets
    assert proj / "AGENTS.md" in plan.native_reads

    # Global instructions -> ~/.pi/agent/AGENTS.md (pi's highest-priority user context file).
    assert home / ".pi" / "agent" / "AGENTS.md" in targets
    assert _src_for(plan, home / ".pi" / "agent" / "AGENTS.md").read_text() == "GLOBAL-INSTR\n"

    # Project skills -> ./.pi/skills, with the SKILL.pi.md overlay merged in.
    assert proj / ".pi" / "skills" in targets
    pskill_md = (_src_for(plan, proj / ".pi" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "license: MIT" in pskill_md
    assert "pi note" in pskill_md

    # Global skills -> ~/.pi/agent/skills.
    assert home / ".pi" / "agent" / "skills" in targets
    assert (_src_for(plan, home / ".pi" / "agent" / "skills") / "gskill" / "SKILL.md").exists()


def test_compile_pi_project_only_injects_skills_not_instructions(tmp_path):
    # No global scope: project AGENTS.md is native (no bind), only project skills bind.
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_pi(project, None, dest)

    assert plan.extra_args == []
    assert _targets(plan) == [proj / ".pi" / "skills"]


def test_compile_pi_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.pi.md").write_text("PI-EXTRA\n")
    (gconf / "AGENTS.opencode.md").write_text("OPENCODE-EXTRA\n")

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_pi(load_source(tmp_path / "noproj"), global_, dest)

    merged = _src_for(plan, pi_agent_dir() / "AGENTS.md").read_text()
    assert "GLOBAL-BASE" in merged
    assert "PI-EXTRA" in merged
    assert "OPENCODE-EXTRA" not in merged  # wrong-harness overlay ignored


def test_pi_agent_dir_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "custom-pi"))
    assert pi_agent_dir() == tmp_path / "custom-pi"


def test_compile_pi_shadows_agents_skills_with_safe_override(tmp_path):
    """compile_pi adds .agents/skills/ to safe_overrides so the launcher tmpfs-shadows it."""
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_pi(project, None, dest)

    # The agent-neutral skills directory is shadowed.
    assert (proj / ".agents" / "skills") in plan.safe_overrides
    # The compiled skills are still injected into .pi/skills/.
    assert (proj / ".pi" / "skills") in _targets(plan)


def test_compile_pi_safe_override_passes_assert_safe(tmp_path):
    """A git-tracked .agents/skills/ source never blocks a pi launch.

    The realistic shape: the agent-neutral source skills are tracked (they are the
    repo's content), compile_pi shadows them with a tmpfs and binds the compiled
    copies at the untracked .pi/skills/ — so the *real* compiled plan must pass
    assert_safe even though the shadowed path is tracked."""
    proj = tmp_path / "proj"
    (proj / ".agents" / "skills" / "pskill").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("x\n")
    (proj / ".agents" / "skills" / "pskill" / "SKILL.md").write_text(
        "---\nname: pskill\ndescription: d\n---\nbody\n"
    )
    subprocess.run(["git", "-C", str(proj), "init"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(proj), "config", "user.email", "test@test"],
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(proj), "config", "user.name", "test"], capture_output=True)
    subprocess.run(["git", "-C", str(proj), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-m", "init"], capture_output=True)

    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_pi(load_source(proj), None, dest)
    assert (proj / ".agents" / "skills") in plan.safe_overrides
    # Should not raise: the tracked path is only tmpfs-shadowed, never bound over.
    assert_safe(proj, plan)


def test_compile_pi_no_shadow_without_project_skills(tmp_path):
    """No .agents/skills/ in the project → no tmpfs shadow (bwrap would otherwise stub
    the missing path into existence on the host)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")

    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_pi(load_source(proj), None, dest)

    assert plan.safe_overrides == set()


def test_compile_codex(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")
    (sk / "SKILL.codex.md").write_text("---\nlicense: MIT\n---\ncodex note\n")

    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTR\n")
    gskills = tmp_path / "ghome" / ".config" / "agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex_config_dir() == home / ".codex"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_codex(project, global_, dest)

    targets = _targets(plan)
    # codex is pure path-discovery — no extra args.
    assert plan.extra_args == []

    # Project AGENTS.md is read natively at ./AGENTS.md — never injected, but recorded.
    assert proj / "AGENTS.md" not in targets
    assert proj / "AGENTS.md" in plan.native_reads

    # Global instructions -> ~/.codex/AGENTS.md (codex's user-scope rules file).
    assert home / ".codex" / "AGENTS.md" in targets
    assert _src_for(plan, home / ".codex" / "AGENTS.md").read_text() == "GLOBAL-INSTR\n"

    # Project skills -> ./.codex/skills, with the SKILL.codex.md overlay merged in.
    assert proj / ".codex" / "skills" in targets
    pskill_md = (_src_for(plan, proj / ".codex" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "license: MIT" in pskill_md
    assert "codex note" in pskill_md

    # Global skills -> ~/.codex/skills.
    assert home / ".codex" / "skills" in targets
    assert (_src_for(plan, home / ".codex" / "skills") / "gskill" / "SKILL.md").exists()


def test_compile_codex_project_only_injects_skills_not_instructions(tmp_path):
    # No global scope: project AGENTS.md is native (no bind), only project skills bind.
    proj = tmp_path / "proj"
    sk = proj / ".agents" / "skills" / "pskill"
    sk.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")
    (sk / "SKILL.md").write_text("---\nname: pskill\ndescription: d\n---\nbody\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_codex(project, None, dest)

    assert plan.extra_args == []
    assert _targets(plan) == [proj / ".codex" / "skills"]


def test_compile_codex_global_agents_harness_overlay_merged(tmp_path, monkeypatch):
    gconf = tmp_path / "gconf" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-BASE\n")
    (gconf / "AGENTS.codex.md").write_text("CODEX-EXTRA\n")
    (gconf / "AGENTS.pi.md").write_text("PI-EXTRA\n")

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=None)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_codex(load_source(tmp_path / "noproj"), global_, dest)

    merged = _src_for(plan, codex_config_dir() / "AGENTS.md").read_text()
    assert "GLOBAL-BASE" in merged
    assert "CODEX-EXTRA" in merged
    assert "PI-EXTRA" not in merged  # wrong-harness overlay ignored


def test_codex_config_dir_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    assert codex_config_dir() == tmp_path / "custom-codex"


def test_build_bwrap_argv_emits_tmpfs_for_safe_overrides():
    """build_bwrap_argv adds --tmpfs for each safe_override before ro-binds."""
    plan = Plan()
    plan.safe_overrides.add(Path("/tmp/a"))
    plan.safe_overrides.add(Path("/tmp/b"))
    argv = build_bwrap_argv(plan, ["pi"])

    idx_a = argv.index("--tmpfs")
    assert argv[idx_a + 1] == "/tmp/a" or argv[idx_a + 1] == "/tmp/b"
    idx_b = argv.index("--tmpfs", idx_a + 1)
    assert argv[idx_b + 1] == "/tmp/a" or argv[idx_b + 1] == "/tmp/b"
    # tmpfs comes before -- (command separator).
    assert argv.index("--tmpfs") < argv.index("--")
