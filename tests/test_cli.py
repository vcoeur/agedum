import json
from pathlib import Path

import pytest

from agedum import __version__
from agedum.cli import main as cli


def _capture_run(monkeypatch):
    captured = {}

    def fake_run(root, plan, command):
        captured["command"] = command
        captured["plan"] = plan
        return 0

    monkeypatch.setattr(cli, "run_virtualfs", fake_run)
    return captured


def _hermetic_sources(monkeypatch):
    """Point the source loaders at a fake non-empty project so ``--dry-run`` compiles
    deterministically instead of reading the host's real project/global sources."""
    project = cli.Source(root=Path("/proj"), agents_md=Path("/proj/AGENTS.md"), skills_dir=None)
    monkeypatch.setattr(cli, "load_source", lambda: project)
    monkeypatch.setattr(cli, "load_global_source", lambda: cli.Source(Path("/g"), None, None))


def _no_launch(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not launch")

    monkeypatch.setattr(cli, "run_virtualfs", boom)


def test_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agedum", "--version"])
    cli.app()
    assert __version__ in capsys.readouterr().out


def test_help_when_no_args(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agedum"])
    cli.app()
    out = capsys.readouterr().out
    assert "usage: agedum" in out
    assert "--wrapper" in out
    assert "provider" in out.lower()
    assert "--build-script" not in out


# --- wrapper mode ---


def test_wrapper_passes_everything_after_dashdash(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr(
        "sys.argv", ["agedum", "--wrapper", "claude", "--", "claude", "--model", "x", "-p"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "--model", "x", "-p"]


def test_wrapper_equals_form(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper=kimi", "--", "kimi", "-p", "hi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "hi"]


def test_removed_legacy_alias_is_not_wrapper_mode(monkeypatch):
    # The `--claude`/`--kimi`/`--opencode` aliases were removed; the bare flag is no
    # longer wrapper mode and is rejected as an unknown provider option.
    monkeypatch.setattr("sys.argv", ["agedum", "--claude", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_wrapper_dry_run_lists_virtual_files_without_launching(monkeypatch, capsys):
    _hermetic_sources(monkeypatch)
    _no_launch(monkeypatch)
    target = Path.home() / ".claude" / "CLAUDE.md"
    plan = cli.Plan(binds=[(Path("/tmp/c/CLAUDE.md"), target)], origins={target: "/proj/AGENTS.md"})
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: plan)
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "claude", "--dry-run", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "virtual files (claude)" in out
    assert "/proj/AGENTS.md → ~/.claude/CLAUDE.md" in out  # source → dest
    assert "command:  claude" in out
    assert "~/.claude/CLAUDE.md" in out
    assert "command:  claude" in out


def test_wrapper_without_harness_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_wrapper_missing_dashdash_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "claude", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


# --- provider mode ---


def _write_provider(tmp_path, name, config, monkeypatch):
    providers = tmp_path / "providers"
    providers.mkdir(exist_ok=True)
    (providers / f"{name}.json").write_text(json.dumps(config))
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    # hermetic env file (empty) so the host's real ~/.config/agents/.env is never read
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))


def test_provider_by_name_launches(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    _write_provider(tmp_path, "mykimi", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "mykimi", "-p", "hi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "hi"]


def test_provider_by_path_launches(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    conf = tmp_path / "custom.json"
    conf.write_text(json.dumps({"harness": "kimi", "config": {"model": "k"}}))
    monkeypatch.setenv("AGENTS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr("sys.argv", ["agedum", str(conf)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "--model", "k"]


def test_provider_dry_run_masks_secrets(monkeypatch, tmp_path, capsys):
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "ds.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "slug": "claude-deepseek-auto",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://api.deepseek.com/anthropic", "model": "m"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-supersecret\n")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))

    # run_virtualfs must NOT be called in dry-run
    def boom(*a, **k):
        raise AssertionError("dry-run must not launch")

    monkeypatch.setattr(cli, "run_virtualfs", boom)
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "ds"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic" in out
    assert "sk-supersecret" not in out  # secret value masked
    assert "***" in out
    assert "unset ANTHROPIC_API_KEY" in out
    assert "command:  claude" in out
    assert "virtual files (claude)" in out


def test_provider_dry_run_with_explicit_env_flag(monkeypatch, tmp_path, capsys):
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "ds.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://x/anthropic", "model": "m"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / "alt.env"
    env_file.write_text("DEEPSEEK_API_KEY=tok\n")
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--env", str(env_file), "--dry-run", "ds"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert "ANTHROPIC_BASE_URL=https://x/anthropic" in capsys.readouterr().out


def test_provider_dry_run_lists_virtual_files_and_extra_args(monkeypatch, tmp_path, capsys):
    _hermetic_sources(monkeypatch)
    _no_launch(monkeypatch)
    plan = cli.Plan(
        binds=[(Path("/tmp/k/skills"), Path("/proj/.kimi/skills"))],
        extra_args=["--agent-file", "/tmp/k/agent.yaml"],
        origins={
            Path("/proj/.kimi/skills"): "/g/.agents/skills",
            Path("/tmp/k/agent.yaml"): "/g/AGENTS.md",
        },
    )
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: plan)
    _write_provider(tmp_path, "mykimi", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "mykimi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "virtual files (kimi)" in out
    assert "/g/.agents/skills → /proj/.kimi/skills" in out  # source → dest
    assert "/g/AGENTS.md → (kimi --agent-file)" in out  # global instructions via agent-file
    assert "appended args: --agent-file /tmp/k/agent.yaml" in out


def test_provider_dry_run_pretty_prints_opencode_config(monkeypatch, tmp_path, capsys):
    _hermetic_sources(monkeypatch)
    _no_launch(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "opencode", lambda project, global_, dest: cli.Plan())
    _write_provider(
        tmp_path,
        "oc",
        {
            "harness": "opencode",
            "config": {"model": "deepseek/deepseek-v4-pro", "effortLevel": "high"},
        },
        monkeypatch,
    )
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "oc"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # OPENCODE_CONFIG_CONTENT is pretty-printed (indented), not a one-line blob.
    assert "export OPENCODE_CONFIG_CONTENT=\n" in out
    assert '"model": "deepseek/deepseek-v4-pro"' in out
    assert '"reasoningEffort": "high"' in out


def test_provider_missing_required_env_returns_1(monkeypatch, tmp_path):
    _write_provider(
        tmp_path,
        "ds",
        {
            "harness": "claude",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"baseUrl": "https://x/anthropic", "model": "m"},
        },
        monkeypatch,
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["agedum", "ds"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 1


def test_provider_unknown_name_returns_1(monkeypatch, tmp_path):
    _write_provider(tmp_path, "exists", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "nope"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 1


def test_provider_required_when_no_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_provider_unknown_option_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--bogus", "x"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2
