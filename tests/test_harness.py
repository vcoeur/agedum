from pathlib import Path

from agedum.harness import claude_config_dir, compile_claude, compile_kimi, kimi_config_dir
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

    skill_md = (skills_src / "demo" / "SKILL.md").read_text()
    assert "name: demo" in skill_md
    assert "description: a demo" in skill_md
    assert "allowed-tools:" in skill_md  # claude overlay frontmatter merged in
    assert "Base body." in skill_md and "Claude note." in skill_md

    assert (skills_src / "demo" / "task1.md").exists()
    assert (skills_src / "demo" / "helper.sh").exists()
    assert not (skills_src / "demo" / "SKILL.kimi.md").exists()


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

    # Instructions -> one merged --agent-file YAML (global + project).
    assert "--agent-file" in plan.extra_args
    yaml_text = Path(plan.extra_args[plan.extra_args.index("--agent-file") + 1]).read_text()
    assert "ROLE_ADDITIONAL:" in yaml_text
    assert "GLOBAL-INSTR" in yaml_text and "PROJECT-INSTR" in yaml_text

    # Global skills -> bound into ~/.kimi/skills.
    assert (home / ".kimi" / "skills") in [t for _, t in plan.binds]
    assert (_src_for(plan, home / ".kimi" / "skills") / "gskill" / "SKILL.md").exists()

    # Project skills -> ./.kimi/skills (project-local bind), kimi overlay applied.
    assert (proj / ".kimi" / "skills") in [t for _, t in plan.binds]
    pskill_md = (_src_for(plan, proj / ".kimi" / "skills") / "pskill" / "SKILL.md").read_text()
    assert "name: pskill" in pskill_md
    assert "kimi note" in pskill_md
    assert "--config" not in plan.extra_args  # no config rewrite
