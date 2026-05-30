from pathlib import Path

from agedum.harness import (
    claude_config_dir,
    compile_claude,
    compile_kimi,
    compile_opencode,
    kimi_config_dir,
    opencode_config_dir,
)
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


def test_load_source_excludes_home_as_project_root(tmp_path, monkeypatch):
    # $HOME holds the *global* source (~/.agents/skills), so find_project_root always
    # matches it via .agents — but home is not a project. load_source must yield an empty
    # project source there, or the global skills get re-injected as project scope.
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
    gskills = tmp_path / "ghome" / ".agents" / "skills"
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
    gskills = tmp_path / "ghome" / ".agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    home = tmp_path / "home"
    (home / ".kimi").mkdir(parents=True)
    (home / ".kimi" / "config.toml").write_text('default_model = "x"\n')
    monkeypatch.setenv("HOME", str(home))
    assert kimi_config_dir() == home / ".kimi"

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_kimi(project, global_, dest)

    # Instructions -> --agent-file holds ONLY the global AGENTS.md. The project
    # AGENTS.md is read natively by kimi (./AGENTS.md), so it is NOT injected here.
    assert "--agent-file" in plan.extra_args
    yaml_text = Path(plan.extra_args[plan.extra_args.index("--agent-file") + 1]).read_text()
    assert "ROLE_ADDITIONAL:" in yaml_text
    assert "GLOBAL-INSTR" in yaml_text
    assert "PROJECT-INSTR" not in yaml_text

    # The natively-read project AGENTS.md is recorded so --dry-run can surface it.
    assert (proj / "AGENTS.md") in plan.native_reads

    # Global skills -> bound into ~/.kimi/skills.
    assert (home / ".kimi" / "skills") in [t for _, t in plan.binds]
    assert (_src_for(plan, home / ".kimi" / "skills") / "gskill" / "SKILL.md").exists()

    # Project skills -> ./.kimi/skills (project-local bind), kimi overlay applied.
    assert (proj / ".kimi" / "skills") in [t for _, t in plan.binds]
    pskill_md = (_src_for(plan, proj / ".kimi" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "kimi note" in pskill_md
    assert "--config" not in plan.extra_args  # no config rewrite


def test_compile_kimi_project_only_injects_no_agent_file(tmp_path):
    # A project with its own AGENTS.md but no global scope: kimi reads ./AGENTS.md
    # natively, so there is nothing to inject — no --agent-file is produced.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("PROJECT-INSTR\n")

    project = load_source(proj)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_kimi(project, None, dest)

    assert "--agent-file" not in plan.extra_args
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
    gskills = tmp_path / "ghome" / ".agents" / "skills"
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

    yaml_text = Path(plan.extra_args[plan.extra_args.index("--agent-file") + 1]).read_text()
    assert "GLOBAL-BASE" in yaml_text
    assert "KIMI-EXTRA" in yaml_text
    assert "CLAUDE-EXTRA" not in yaml_text  # wrong-harness overlay ignored


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
