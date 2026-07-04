import shutil
import subprocess
from pathlib import Path

import pytest

from agedum.harness import Plan, Sandbox, compile_claude
from agedum.launcher import (
    LauncherError,
    _effective_binds,
    _ensure_writable_dirs,
    _resolve_rw,
    assert_safe,
    build_bwrap_argv,
    run_virtualfs,
    writable_roots,
)
from agedum.sources import load_source


def _binds(argv, flag):
    """Extract the (src, dest) string pairs mounted with ``flag`` from a bwrap argv."""
    pairs = []
    index = 0
    while index < len(argv):
        if argv[index] == flag:
            pairs.append((argv[index + 1], argv[index + 2]))
            index += 3
        else:
            index += 1
    return pairs


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


def test_assert_safe_ignores_tracked_sibling_in_skills_dir(tmp_path):
    # The per-child overlay never masks a sibling agedum does not ship, so a tracked,
    # hand-authored skill in the target dir must not block the launch.
    _git_init(tmp_path)
    handmade = tmp_path / ".claude" / "skills" / "handmade"
    handmade.mkdir(parents=True)
    (handmade / "SKILL.md").write_text("hand-authored, deliberately versioned\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "x"], check=True)

    compiled = tmp_path / "compiled-skills"
    (compiled / "demo").mkdir(parents=True)
    plan = Plan(binds=[(compiled, tmp_path / ".claude" / "skills")])
    # Should not raise: agedum only binds .claude/skills/demo, never .../handmade.
    assert_safe(tmp_path, plan)


def test_assert_safe_refuses_tracked_same_named_skill(tmp_path):
    # A tracked skill agedum *would* bind over is still refused.
    _git_init(tmp_path)
    demo = tmp_path / ".claude" / "skills" / "demo"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text("tracked\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "x"], check=True)

    compiled = tmp_path / "compiled-skills"
    (compiled / "demo").mkdir(parents=True)
    plan = Plan(binds=[(compiled, tmp_path / ".claude" / "skills")])
    with pytest.raises(LauncherError):
        assert_safe(tmp_path, plan)


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


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_virtualfs_sweeps_safe_override_stubs(tmp_path):
    # bwrap creates the mountpoint dirs for a --tmpfs shadow just like for a bind; a
    # shadow over a path that did not exist must not leave stub dirs on the host.
    proj = tmp_path / "proj"
    proj.mkdir()
    plan = Plan(safe_overrides={proj / ".agents" / "skills"})

    rc = run_virtualfs(proj, plan, ["true"])

    assert rc == 0
    assert not (proj / ".agents").exists()  # both stub levels swept


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


def test_run_virtualfs_stdin_handling(monkeypatch, tmp_path):
    import subprocess as sp

    import agedum.launcher as launcher_mod

    captured = {}

    class _Result:
        returncode = 0

    def fake_run(argv, *a, **k):
        captured["stdin"] = k.get("stdin")
        return _Result()

    monkeypatch.setattr(launcher_mod.subprocess, "run", fake_run)

    # Default (interactive): the child inherits stdin (no override).
    run_virtualfs(tmp_path, Plan(), ["opencode"])
    assert captured["stdin"] is None

    # close_stdin (a non-interactive --run): stdin is /dev/null so the harness can't block.
    run_virtualfs(tmp_path, Plan(), ["opencode", "run", "go"], close_stdin=True)
    assert captured["stdin"] is sp.DEVNULL


# --- write-confinement sandbox ---


def test_build_bwrap_argv_default_is_full_read_write(tmp_path):
    # No sandbox (or a disabled one) keeps the legacy full read-write host bind.
    plan = Plan()
    argv = build_bwrap_argv(plan, ["claude"], sandbox=Sandbox(enabled=False), project_root=tmp_path)
    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert "--bind" not in argv


def test_build_bwrap_argv_sandbox_confines(tmp_path):
    plan = Plan(binds=[(tmp_path / "c.md", tmp_path / "CLAUDE.md")])
    sandbox = Sandbox(enabled=True, read_write=("/var/data",))
    argv = build_bwrap_argv(plan, ["claude"], sandbox=sandbox, project_root=tmp_path)
    # Host read-only, with device / process / scratch mounts a read-only root lacks.
    assert argv[:10] == [
        "bwrap", "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
    ]  # fmt: skip
    writable = _binds(argv, "--bind")
    assert (str(tmp_path), str(tmp_path)) in writable  # project root writable
    assert ("/var/data", "/var/data") in writable  # declared rw path writable
    # The compiled file is still injected read-only, over the now-writable project root.
    assert (str(tmp_path / "c.md"), str(tmp_path / "CLAUDE.md")) in _binds(argv, "--ro-bind")
    assert argv[-2:] == ["--", "claude"]


def test_build_bwrap_argv_sandbox_requires_project_root():
    with pytest.raises(LauncherError):
        build_bwrap_argv(Plan(), ["x"], sandbox=Sandbox(enabled=True), project_root=None)


def test_writable_roots_unions_project_injection_parents_and_rw(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # A global-scope injection (e.g. ~/.claude/CLAUDE.md) lives outside the project: its
    # nearest existing ancestor must be writable so bwrap can create the mount point and the
    # harness can persist its own state.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    plan = Plan(binds=[(tmp_path / "g.md", home / ".claude" / "CLAUDE.md")])
    sandbox = Sandbox(enabled=True, read_write=("/data",))
    roots = writable_roots(plan, sandbox, proj)
    assert proj in roots
    assert home / ".claude" in roots
    assert Path("/data") in roots


def test_writable_roots_drops_paths_nested_under_the_project(tmp_path):
    # A read_write path inside the project root is redundant — the project root bind covers it.
    sandbox = Sandbox(enabled=True, read_write=("${PROJECT_ROOT}/out", "/elsewhere"))
    roots = writable_roots(Plan(), sandbox, tmp_path)
    assert tmp_path in roots
    assert tmp_path / "out" not in roots
    assert Path("/elsewhere") in roots


def test_writable_roots_expands_glob_read_write(tmp_path):
    # A glob read_write entry makes every matching child writable (e.g. `~/src/*` → each repo
    # under ~/src), without binding the glob parent itself.
    proj = tmp_path / "proj"
    proj.mkdir()
    src = tmp_path / "src"
    (src / "one").mkdir(parents=True)
    (src / "two").mkdir()
    sandbox = Sandbox(enabled=True, read_write=(str(src / "*"),))
    roots = writable_roots(Plan(), sandbox, proj)
    assert src / "one" in roots
    assert src / "two" in roots
    assert src not in roots


def test_writable_roots_includes_harness_state_dirs(tmp_path):
    # A harness's own state dir (Plan.writable_dirs) is writable even with no bind landing under
    # it and no matching read_write entry — this is what keeps ~/.cline/data writable for Cline.
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "home" / ".cline"
    sandbox = Sandbox(enabled=True, read_write=())
    roots = writable_roots(Plan(writable_dirs=[state]), sandbox, proj)
    assert state in roots


def test_writable_roots_drops_state_dir_under_a_broader_rw_grant(tmp_path):
    # A harness config dir nested under a config-granted parent (e.g. ~/.config/opencode under a
    # readWrite ~/.config) is redundant and folded out — the parent bind already covers it.
    proj = tmp_path / "proj"
    proj.mkdir()
    config = tmp_path / "home" / ".config"
    opencode = config / "opencode"
    sandbox = Sandbox(enabled=True, read_write=(str(config),))
    roots = writable_roots(Plan(writable_dirs=[opencode]), sandbox, proj)
    assert config in roots
    assert opencode not in roots


def test_ensure_writable_dirs_creates_missing(tmp_path):
    # bwrap can't bind a missing path; the launcher pre-creates each declared state dir.
    missing = tmp_path / "home" / ".cline"
    assert not missing.exists()
    _ensure_writable_dirs(Plan(writable_dirs=[missing]))
    assert missing.is_dir()


def test_resolve_rw_expands_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("AGEDUM_RW_TEST_DIR", "/env/dir")
    # A plain (non-glob) template resolves to a single literal path, kept whether or not it
    # exists.
    assert _resolve_rw("${PROJECT_ROOT}/out", tmp_path) == [tmp_path / "out"]
    assert _resolve_rw("$AGEDUM_RW_TEST_DIR/x", tmp_path) == [Path("/env/dir/x")]
    assert _resolve_rw("~/data", tmp_path) == [Path.home() / "data"]
    assert _resolve_rw(str(tmp_path / "missing"), tmp_path) == [tmp_path / "missing"]


def test_resolve_rw_expands_globs(tmp_path):
    src = tmp_path / "src"
    (src / "alpha").mkdir(parents=True)
    (src / "beta").mkdir()
    (src / "note.txt").write_text("x")
    # `~/src/*`-style entry → every existing immediate child, sorted; the glob's parent dir
    # itself is not added.
    assert _resolve_rw(str(src / "*"), tmp_path) == [src / "alpha", src / "beta", src / "note.txt"]
    # An unmatched glob contributes nothing (no bogus literal path with a `*` in it).
    assert _resolve_rw(str(src / "nope" / "*"), tmp_path) == []


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_virtualfs_sandbox_confines_writes(tmp_path):
    # The decisive behaviour: writes land inside the working set and nowhere else.
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sandbox = Sandbox(enabled=True)

    rc_in = run_virtualfs(proj, Plan(), ["touch", str(proj / "inside.txt")], sandbox=sandbox)
    assert rc_in == 0
    assert (proj / "inside.txt").exists()  # the in-project write reached the host

    rc_out = run_virtualfs(proj, Plan(), ["touch", str(outside / "leak.txt")], sandbox=sandbox)
    assert rc_out != 0
    assert not (outside / "leak.txt").exists()  # the out-of-set write never reached the host


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_virtualfs_sandbox_allows_declared_rw_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    sandbox = Sandbox(enabled=True, read_write=(str(data),))

    rc = run_virtualfs(proj, Plan(), ["touch", str(data / "ok.txt")], sandbox=sandbox)
    assert rc == 0
    assert (data / "ok.txt").exists()  # a write into a declared rw dir reached the host


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_virtualfs_sandbox_allows_harness_state_write(tmp_path):
    # The Cline repro in miniature: a harness state dir that does not yet exist is created and
    # made writable, so the harness can persist state (e.g. providers.json) instead of EROFS.
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "home" / ".cline"  # missing before the run
    sandbox = Sandbox(enabled=True)
    plan = Plan(writable_dirs=[state])

    rc = run_virtualfs(proj, plan, ["touch", str(state / "providers.json")], sandbox=sandbox)
    assert rc == 0
    assert state.is_dir()  # the state dir was pre-created on the host
    assert (state / "providers.json").exists()  # a write into the state dir reached the host
