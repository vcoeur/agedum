from agedum.harness import compile_claude
from agedum.sources import load_source


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
    plan = compile_claude(src, dest)

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
    plan = compile_claude(src, dest)
    assert plan.tmpfs == []
    assert plan.binds == []
