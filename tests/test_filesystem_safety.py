import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agedum import harness, launcher
from agedum.sources import Source, load_source


def git(root, *arguments, env=None):
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, env=env
    ).stdout


def skill(directory, name="sample", body="base"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: example\n---\n{body}\n")


def test_fresh_cline_grants_only_prepared_parents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLINE_DATA_DIR", raising=False)
    monkeypatch.chdir(project)
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("global")
    plan = harness.compile_cline(
        Source(project, None, None), Source(home, instructions, None), tmp_path / "compiled"
    )
    sandbox = harness.Sandbox(enabled=True)
    before = launcher.writable_roots(plan, sandbox)
    assert not (home / ".agents").exists()
    launcher._ensure_writable_dirs(plan)
    assert before == launcher.writable_roots(plan, sandbox)
    assert set(before) == {project, home / ".agents", home / ".cline"}
    assert all(path.is_dir() for path in before)
    assert home not in before
    assert home in launcher.writable_roots(
        plan, harness.Sandbox(enabled=True, read_write=(str(home),))
    )  # Explicit broad grants remain authored policy.


def test_injection_parent_symlink_cannot_alias_whole_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".agents").symlink_to(home, target_is_directory=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    plan = harness.Plan(binds=[(tmp_path / "compiled", home / ".agents/AGENTS.md")])
    with pytest.raises(launcher.LauncherError, match="symlinked injection parent"):
        launcher.writable_roots(plan, harness.Sandbox(enabled=True))
    with pytest.raises(launcher.LauncherError, match="symlinked injection parent"):
        launcher._ensure_writable_dirs(plan)


def test_nested_source_and_transcript_targets_use_actual_git(tmp_path):
    root = tmp_path / "repo"
    nested = root / "subproject"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("source")
    (nested / "CLAUDE.md").write_text("tracked")
    local = nested / ".claude" / "settings.local.json"
    local.parent.mkdir()
    local.write_text("{}")
    git(root, "init", "-q")
    git(root, "add", "-f", "subproject/CLAUDE.md", "subproject/.claude/settings.local.json")
    source = load_source(nested)
    assert source.root == nested
    plan = harness.compile_claude(source, None, tmp_path / "compiled")
    assert local not in [target for _, target in plan.binds]
    with pytest.raises(launcher.LauncherError, match="git-tracked"):
        launcher.assert_safe(source.root, plan)


def test_global_target_in_other_repository_is_guarded(tmp_path):
    repository = tmp_path / "other"
    repository.mkdir()
    target = repository / "CLAUDE.md"
    target.write_text("tracked")
    git(repository, "init", "-q")
    git(repository, "add", "CLAUDE.md")
    with pytest.raises(launcher.LauncherError, match="git-tracked"):
        launcher.assert_safe(tmp_path, harness.Plan(binds=[(tmp_path / "compiled", target)]))


@pytest.mark.parametrize("operation", ["ownership", "index"])
def test_git_inspection_errors_fail_closed(tmp_path, monkeypatch, operation):
    original = subprocess.run

    def fail(arguments, **kwargs):
        if "rev-parse" in arguments and operation == "index":
            return subprocess.CompletedProcess(arguments, 0, str(tmp_path).encode() + b"\n", b"")
        if arguments[0] == "git":
            return subprocess.CompletedProcess(arguments, 128, b"", b"fatal: simulated index error")
        return original(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(launcher.LauncherError, match="cannot inspect Git"):
        launcher.assert_safe(
            tmp_path, harness.Plan(binds=[(tmp_path / "out", tmp_path / "target")])
        )


def test_shadow_of_tracked_tree_is_rejected(tmp_path):
    skill(tmp_path / ".agents" / "skills" / "sample")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".agents")
    with pytest.raises(launcher.LauncherError, match="git-tracked"):
        launcher.assert_safe(tmp_path, harness.Plan(safe_overrides={tmp_path / ".agents/skills"}))


def test_pi_real_index_staging_keeps_source_and_settings(tmp_path):
    root = tmp_path / "repo"
    source_skill = root / ".agents" / "skills" / "sample"
    skill(source_skill)
    (source_skill / "SKILL.pi.md").write_text("overlay\n")
    (root / "AGENTS.md").write_text("source")
    (root / ".gitignore").write_text(".pi/\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    initial = git(root, "ls-files", "--stage")
    (root / ".pi").mkdir()
    (root / ".pi/settings.json").write_text(json.dumps({"skills": ["manual"], "theme": "light"}))
    plan = harness.compile_pi(load_source(root), None, tmp_path / "compiled")
    launcher.assert_safe(root, plan)
    assert not plan.safe_overrides
    # Simulate only the effective overlays, not a different index or a hidden source tree.
    for source, target in launcher._effective_binds(plan):
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
    git(root, "add", "-u")
    assert git(root, "ls-files", "--stage") == initial
    assert "overlay" in (root / ".pi/skills/sample/SKILL.md").read_text()
    settings = json.loads((root / ".pi/settings.json").read_text())
    assert settings["theme"] == "light"
    assert settings["skills"] == ["manual", f"-{source_skill / 'SKILL.md'}"]


def test_pi_tracked_settings_require_technical_decision(tmp_path):
    skill(tmp_path / ".agents/skills/sample")
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".pi/settings.json").write_text("{}")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".pi/settings.json")
    with pytest.raises(launcher.LauncherError, match="untracked"):
        harness.compile_pi(load_source(tmp_path), None, tmp_path / "compiled")


ASSETS = Path(__file__).resolve().parents[1] / "agedum/assets"


def node_capture(tmp_path, engine, sidecar, *, mode="user", input_data=None, foreign=False):
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CONDASH_TRANSCRIPT_FILE": str(sidecar),
    }
    script = ASSETS / (
        "claude/emit-transcript.mjs" if engine == "claude" else "opencode/transcript-osc.js"
    )
    foreign_setup = (
        """
      const original = fs.fstatSync;
      fs.fstatSync = (...args) => {
        const stat = original(...args); stat.uid = process.getuid() + 1; return stat;
      };
    """
        if foreign
        else ""
    )
    if engine == "claude" and not foreign:
        command = ["node", str(script), mode]
    else:
        # A fresh process imports the real plugin/hook with only synthetic filesystem events.
        code = f"import fs from 'node:fs'; {foreign_setup}\n"
        if engine == "claude":
            code += f"process.argv[2] = {json.dumps(mode)};\n"
            code += f"await import({json.dumps(script.as_uri())});"
        else:
            code += (
                f"const {{TranscriptOscPlugin}} = await import({json.dumps(script.as_uri())});\n"
            )
            code += """
              const plugin = await TranscriptOscPlugin();
              await plugin['chat.message']({sessionID:'fake'}, {
                parts:[{id:'user', type:'text', text:'PRIVATE-FAKE'}]
              });
              await plugin.event({event:{type:'message.part.updated', properties:{part:{
                id:'reason', type:'reasoning', text:'FAKE-THOUGHT', time:{end:1}
              }}}});
            """
        command = ["node", "--input-type=module", "-e", code]
    return subprocess.run(
        command,
        input=json.dumps(input_data or {"prompt": "PRIVATE-FAKE", "session_id": "fake"}),
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
        check=True,
    )


@pytest.mark.parametrize("engine", ["claude", "opencode"])
def test_node_sidecar_private_creation_and_append(tmp_path, engine):
    sidecar = tmp_path / "private/nested/transcript.ndjson"
    node_capture(tmp_path, engine, sidecar)
    node_capture(tmp_path, engine, sidecar)
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert sidecar.parent.stat().st_mode & 0o777 == 0o700
    assert sidecar.parent.parent.stat().st_mode & 0o777 == 0o700
    frames = [json.loads(line) for line in sidecar.read_text().splitlines()]
    assert frames[0]["text"] == "PRIVATE-FAKE"
    assert len(frames) == (2 if engine == "claude" else 4)


@pytest.mark.parametrize("engine", ["claude", "opencode"])
@pytest.mark.parametrize(
    "unsafe",
    [
        "file-symlink",
        "directory-symlink",
        "file-mode",
        "directory-mode",
        "foreign",
        "hardlink",
        "fifo",
    ],
)
def test_node_sidecar_rejects_unsafe_existing_storage(tmp_path, engine, unsafe):
    storage = tmp_path / "private"
    storage.mkdir(mode=0o700)
    sidecar = storage / "transcript"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("UNCHANGED")
    if unsafe == "file-symlink":
        sidecar.symlink_to(sentinel)
    elif unsafe == "directory-symlink":
        storage.rmdir()
        storage.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "file-mode":
        sidecar.write_text("UNCHANGED")
        sidecar.chmod(0o644)
    elif unsafe == "directory-mode":
        storage.chmod(0o755)
    elif unsafe == "hardlink":
        sentinel.chmod(0o600)
        os.link(sentinel, sidecar)
    elif unsafe == "fifo":
        os.mkfifo(sidecar, 0o600)
    node_capture(tmp_path, engine, sidecar, foreign=unsafe == "foreign")
    assert sentinel.read_text() == "UNCHANGED"
    if unsafe == "file-mode":
        assert sidecar.read_text() == "UNCHANGED"
    if unsafe in {"directory-mode", "foreign", "directory-symlink"}:
        assert not sidecar.exists()


def checkpoint_fixture(tmp_path):
    transcript = tmp_path / "fake.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "FAKE-ANSWER"},
                        {"type": "thinking", "thinking": "FAKE-THOUGHT"},
                    ]
                },
            }
        )
        + "\n"
    )
    data = {"session_id": "fake", "transcript_path": str(transcript)}
    identity = json.dumps(["fake", str(transcript)], separators=(",", ":"))
    directory = tmp_path / f"agedum-claude-transcript-{os.getuid()}"
    name = hashlib.sha256(identity.encode()).hexdigest() + ".offset"
    return transcript, data, directory, name


def test_checkpoint_atomic_private_and_duplicate_stop(tmp_path):
    transcript, data, directory, name = checkpoint_fixture(tmp_path)
    sidecar = tmp_path / "private/transcript"
    node_capture(tmp_path, "claude", sidecar, mode="stop", input_data=data)
    checkpoint = directory / name
    assert checkpoint.read_text() == str(transcript.stat().st_size)
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    node_capture(tmp_path, "claude", sidecar, mode="stop", input_data=data)
    assert len(sidecar.read_text().splitlines()) == 2
    assert [path.name for path in directory.iterdir()] == [name]


@pytest.mark.parametrize(
    "unsafe", ["checkpoint-symlink", "directory-symlink", "directory-mode", "foreign"]
)
def test_checkpoint_unsafe_storage_never_truncates(tmp_path, unsafe):
    _, data, directory, name = checkpoint_fixture(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("DO-NOT-OVERWRITE")
    if unsafe == "directory-symlink":
        directory.symlink_to(tmp_path, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        if unsafe == "checkpoint-symlink":
            (directory / name).symlink_to(sentinel)
        elif unsafe == "directory-mode":
            directory.chmod(0o777)
    sidecar = tmp_path / "private/transcript"
    node_capture(
        tmp_path, "claude", sidecar, mode="stop", input_data=data, foreign=unsafe == "foreign"
    )
    assert sentinel.read_text() == "DO-NOT-OVERWRITE"
    assert not sidecar.exists()


def test_checkpoint_concurrent_stops_are_serialized(tmp_path):
    _, data, directory, name = checkpoint_fixture(tmp_path)
    sidecar = tmp_path / "private/transcript"
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _: node_capture(tmp_path, "claude", sidecar, mode="stop", input_data=data),
                range(8),
            )
        )
    assert len(sidecar.read_text().splitlines()) == 2
    assert [path.name for path in directory.iterdir()] == [name]


@pytest.mark.parametrize("readonly", [False, True])
def test_shared_index_shadow_simulation_is_unsafe_even_readonly(tmp_path, readonly):
    root = tmp_path / "repo"
    skill(root / ".agents/skills/sample")
    git(root, "init", "-q")
    git(root, "add", ".")
    alternative = tmp_path / "hidden-view"
    alternative.mkdir()
    hidden = alternative / ".agents/skills"
    hidden.mkdir(parents=True)
    hidden.chmod(0o555 if readonly else 0o755)
    environment = {**os.environ, "GIT_WORK_TREE": str(alternative)}
    git(root, "add", "-u", env=environment)
    assert git(root, "ls-files") == b""  # No host file was deleted, but the real index lost it.
    assert (root / ".agents/skills/sample/SKILL.md").exists()


@pytest.mark.parametrize("nested", [False, True])
def test_pi_installed_native_discovery_keeps_manual_and_overlay_skills(
    tmp_path, monkeypatch, nested
):
    executable = shutil.which("pi")
    if executable is None:
        pytest.skip("Pi native discovery runtime requires the installed Pi distribution")
    module = Path(executable).resolve().parent / "core/package-manager.js"
    if not module.exists():
        pytest.skip("Pi package-manager module not available in this distribution")
    root = tmp_path / "repo"
    source = root / ".agents/skills/sample"
    skill(source)
    (source / "SKILL.pi.md").write_text("overlay\n")
    launch_root = root / "nested" if nested else root
    skill(launch_root / ".pi/skills/manual", "manual")
    home = tmp_path / "home"
    agent_dir = home / ".pi/agent"
    skill(agent_dir / "skills/global-manual", "global-manual")
    skill(home / ".agents/skills/legacy-manual", "legacy-manual")
    package = tmp_path / "local-package"
    skill(package / "skills/package-manual", "package-manual")
    (package / "package.json").write_text(json.dumps({"pi": {"skills": ["skills"]}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.chdir(launch_root)
    plan = harness.compile_pi(load_source(root), None, tmp_path / "compiled")
    for compiled, target in launcher._effective_binds(plan):
        target.parent.mkdir(parents=True, exist_ok=True)
        if compiled.is_dir():
            shutil.copytree(compiled, target)
        else:
            shutil.copyfile(compiled, target)
    settings = json.loads((launch_root / ".pi/settings.json").read_text())
    settings["packages"] = [str(package)]
    code = f"""
      import {{DefaultPackageManager}} from {json.dumps(module.as_uri())};
      const manager = new DefaultPackageManager({{
        cwd: {json.dumps(str(launch_root))}, agentDir: {json.dumps(str(agent_dir))},
        settingsManager: {{getGlobalSettings: () => ({{}}),
          getProjectSettings: () => ({json.dumps(settings)})}}
      }});
      console.log(JSON.stringify((await manager.resolve()).skills));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": os.environ["PATH"], "HOME": str(home), "PI_OFFLINE": "1"},
    )
    resources = json.loads(result.stdout)
    enabled = {entry["path"] for entry in resources if entry["enabled"]}
    assert str(source / "SKILL.md") not in enabled
    assert str(launch_root / ".pi/skills/sample/SKILL.md") in enabled
    assert str(launch_root / ".pi/skills/manual/SKILL.md") in enabled
    assert str(agent_dir / "skills/global-manual/SKILL.md") in enabled
    assert str(home / ".agents/skills/legacy-manual/SKILL.md") in enabled
    assert str(package / "skills/package-manual/SKILL.md") in enabled
