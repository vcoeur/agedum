import shutil
import subprocess

import pytest

from agedum.harness import Plan, compile_claude
from agedum.launcher import LauncherError, assert_safe, build_bwrap_argv, run_virtualfs
from agedum.sources import load_source


def _git_init(path):
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def test_build_bwrap_argv_orders_tmpfs_before_binds(tmp_path):
    plan = Plan(
        tmpfs=[".claude"],
        binds=[(tmp_path / "c.md", "CLAUDE.md"), (tmp_path / "sk", ".claude/skills")],
    )
    argv = build_bwrap_argv(tmp_path, plan, ["claude", "-p"])
    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert argv[-3:] == ["--", "claude", "-p"]
    assert argv.index(str(tmp_path / ".claude")) < argv.index(str(tmp_path / ".claude/skills"))


def test_assert_safe_refuses_tracked_target(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("real tracked\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "CLAUDE.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "x"], check=True)
    plan = Plan(binds=[(tmp_path / "compiled.md", "CLAUDE.md")])
    with pytest.raises(LauncherError):
        assert_safe(tmp_path, plan)


def test_assert_safe_allows_untracked(tmp_path):
    _git_init(tmp_path)
    assert_safe(tmp_path, Plan(binds=[(tmp_path / "compiled.md", "CLAUDE.md")]))


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_virtualfs_injects_then_sweeps_stubs(tmp_path):
    proj = tmp_path / "proj"
    skill = proj / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("INJECTED-INSTRUCTIONS\n")
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nb\n")
    (proj / ".gitignore").write_text("CLAUDE.md\n.claude/\n")
    _git_init(proj)
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-qm", "init"], check=True)

    src = load_source(proj)
    dest = tmp_path / "compiled"
    dest.mkdir()
    plan = compile_claude(src, None, dest)

    marker = tmp_path / "marker.txt"
    cmd = ["bash", "-c", f"cd {proj} && cat CLAUDE.md > {marker} && ls .claude/skills >> {marker}"]
    rc = run_virtualfs(proj, plan, cmd)

    assert rc == 0
    out = marker.read_text()
    assert "INJECTED-INSTRUCTIONS" in out  # child saw the injected CLAUDE.md
    assert "demo" in out  # child saw the injected skill

    # stubs swept — nothing injected leaks onto the host
    assert not (proj / "CLAUDE.md").exists()
    assert not (proj / ".claude").exists()

    # the real repo is untouched
    status = subprocess.run(
        ["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert status.strip() == ""
