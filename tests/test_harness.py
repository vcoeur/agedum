from agedum.harness import compile_claude
from agedum.sources import Source, load_source


def test_compile_claude_layout_and_overlay(tmp_path):
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

    # Instructions: AGENTS.md -> CLAUDE.md
    assert (dest / "CLAUDE.md").read_text() == "# project instructions\n"

    skill_md = (dest / ".claude" / "skills" / "demo" / "SKILL.md").read_text()
    assert "name: demo" in skill_md
    assert "description: a demo" in skill_md
    assert "allowed-tools:" in skill_md  # claude overlay frontmatter merged in
    assert "Base body." in skill_md and "Claude note." in skill_md

    # Task files + scripts copied; other-harness overlay skipped.
    assert (dest / ".claude" / "skills" / "demo" / "task1.md").exists()
    assert (dest / ".claude" / "skills" / "demo" / "helper.sh").exists()
    assert not (dest / ".claude" / "skills" / "demo" / "SKILL.kimi.md").exists()

    assert plan.tmpfs == [".claude"]
    targets = [t for _, t in plan.binds]
    assert "CLAUDE.md" in targets
    assert ".claude/skills" in targets


def test_compile_with_no_source_is_empty(tmp_path):
    (tmp_path / ".git").mkdir()
    src = load_source(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(src, None, dest)
    assert plan.tmpfs == []
    assert plan.binds == []


def test_compile_merges_global_and_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".agents" / "skills" / "pskill").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("PROJECT-INSTRUCTIONS\n")
    (proj / ".agents" / "skills" / "pskill" / "SKILL.md").write_text(
        "---\nname: pskill\ndescription: d\n---\n"
    )

    gconf = tmp_path / "global-config" / "agents"
    gconf.mkdir(parents=True)
    (gconf / "AGENTS.md").write_text("GLOBAL-INSTRUCTIONS\n")
    gskills = tmp_path / "global-home" / ".agents" / "skills"
    (gskills / "gskill").mkdir(parents=True)
    (gskills / "gskill" / "SKILL.md").write_text("---\nname: gskill\ndescription: d\n---\n")

    project = load_source(proj)
    global_ = Source(root=tmp_path, agents_md=gconf / "AGENTS.md", skills_dir=gskills)
    dest = tmp_path / "out"
    dest.mkdir()
    plan = compile_claude(project, global_, dest)

    claude_md = (dest / "CLAUDE.md").read_text()
    # global instructions come first, project after
    assert claude_md.index("GLOBAL-INSTRUCTIONS") < claude_md.index("PROJECT-INSTRUCTIONS")
    # both global and project skills land under .claude/skills/
    assert (dest / ".claude" / "skills" / "gskill" / "SKILL.md").exists()
    assert (dest / ".claude" / "skills" / "pskill" / "SKILL.md").exists()
    assert plan.tmpfs == [".claude"]
