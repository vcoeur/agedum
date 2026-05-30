import shutil
import subprocess

import pytest

from agedum.harness import Plan, compile_claude
from agedum.launcher import (
    LauncherError,
    _effective_binds,
    assert_safe,
    build_bwrap_argv,
    run_virtualfs,
)
from agedum.sources import load_source


def _git_init(path):
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def test_build_bwrap_argv_binds_absolute_targets(tmp_path):
    plan = Plan(
        binds=[
            (tmp_path / "c.md", tmp_path / "CLAUDE.md"),
            (tmp_path / "sk", tmp_path / ".claude" / "skills"),
        ]
    )
    argv = build_bwrap_argv(plan, ["claude", "-p"])
    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert argv[-3:] == ["--", "claude", "-p"]
    assert str(tmp_path / "CLAUDE.md") in argv
    assert str(tmp_path / ".claude" / "skills") in argv


def test_effective_binds_expands_directory_one_level(tmp_path):
    # A directory bind overlays each child at target/<child>; a file bind passes through.
    skills = tmp_path / "sk"
    (skills / "a").mkdir(parents=True)
    (skills / "b").mkdir(parents=True)
    plan = Plan(
        binds=[
            (tmp_path / "c.md", tmp_path / "CLAUDE.md"),
            (skills, tmp_path / ".claude" / "skills"),
        ]
    )
    effective = set(_effective_binds(plan))
    assert (tmp_path / "c.md", tmp_path / "CLAUDE.md") in effective  # file passthrough
    assert (skills / "a", tmp_path / ".claude" / "skills" / "a") in effective
    assert (skills / "b", tmp_path / ".claude" / "skills" / "b") in effective
    # the whole-dir target is never bound directly — that would mask siblings
    assert (skills, tmp_path / ".claude" / "skills") not in effective


def test_build_bwrap_argv_overlays_skills_per_child(tmp_path):
    skills = tmp_path / "sk"
    (skills / "demo").mkdir(parents=True)
    plan = Plan(binds=[(skills, tmp_path / ".claude" / "skills")])
    argv = build_bwrap_argv(plan, ["claude"])
    assert str(tmp_path / ".claude" / "skills" / "demo") in argv
    # the parent skills dir itself is not a bind target (so on-disk siblings stay visible)
    assert str(tmp_path / ".claude" / "skills") not in argv


def test_assert_safe_refuses_tracked_target(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("real tracked\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "CLAUDE.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "x"], check=True)
    plan = Plan(binds=[(tmp_path / "compiled.md", tmp_path / "CLAUDE.md")])
    with pytest.raises(LauncherError):
        assert_safe(tmp_path, plan)


def test_assert_safe_ignores_targets_outside_the_project(tmp_path):
    _git_init(tmp_path)
    # a global-scope target (e.g. ~/.claude) is not part of this repo, so never tracked
    outside = tmp_path.parent / "claude-home" / "CLAUDE.md"
    assert_safe(tmp_path, Plan(binds=[(tmp_path / "compiled.md", outside)]))


def test_assert_safe_allows_untracked(tmp_path):
    _git_init(tmp_path)
    assert_safe(tmp_path, Plan(binds=[(tmp_path / "compiled.md", tmp_path / "CLAUDE.md")]))


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


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_virtualfs_overlay_preserves_hand_authored_sibling(tmp_path):
    # A skill already in the target skills dir, not shipped by agedum, must stay visible —
    # the overlay binds only agedum's skills per-child, never the whole dir.
    proj = tmp_path / "proj"
    skill = proj / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nb\n")
    (proj / "AGENTS.md").write_text("X\n")
    (proj / ".gitignore").write_text("CLAUDE.md\n.claude/\n")
    # a pre-existing, hand-authored skill living in the real target dir
    user_skill = proj / ".claude" / "skills" / "user-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("---\nname: user-skill\ndescription: u\n---\nu\n")
    _git_init(proj)
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-qm", "init"], check=True)

    src = load_source(proj)
    dest = tmp_path / "compiled"
    dest.mkdir()
    plan = compile_claude(src, None, dest)

    marker = tmp_path / "marker.txt"
    cmd = ["bash", "-c", f"cd {proj} && ls .claude/skills > {marker}"]
    rc = run_virtualfs(proj, plan, cmd)

    assert rc == 0
    listed = marker.read_text()
    assert "demo" in listed  # agedum's skill was overlaid in
    assert "user-skill" in listed  # the hand-authored sibling survived

    # the shipped-only skill left no stub; the pre-existing user skill is untouched
    assert not (proj / ".claude" / "skills" / "demo").exists()
    assert (proj / ".claude" / "skills" / "user-skill" / "SKILL.md").exists()


def test_run_appends_plan_extra_args_after_command(monkeypatch, tmp_path):
    import agedum.launcher as launcher_mod

    captured = {}

    class _Result:
        returncode = 0

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        return _Result()

    monkeypatch.setattr(launcher_mod.subprocess, "run", fake_run)
    plan = Plan(extra_args=["--agent-file", "/tmp/a.yaml", "--config", "{}"])
    rc = run_virtualfs(tmp_path, plan, ["kimi", "-p", "hi"])

    assert rc == 0
    argv = captured["argv"]
    tail = argv[argv.index("--") + 1 :]
    assert tail == ["kimi", "-p", "hi", "--agent-file", "/tmp/a.yaml", "--config", "{}"]
