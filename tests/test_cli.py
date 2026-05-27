import json

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


def test_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agedum", "--version"])
    cli.app()
    assert __version__ in capsys.readouterr().out


def test_help_when_no_args(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agedum"])
    cli.app()
    out = capsys.readouterr().out
    assert "usage: agedum" in out
    assert "--wrapper" in out and "--build-script" in out


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


def test_opencode_wrapper(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "opencode", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "opencode", "--", "opencode", "run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["opencode", "run"]


def test_legacy_alias_still_works_with_deprecation(monkeypatch, capsys):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--claude", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude"]
    assert "deprecated" in capsys.readouterr().err


def test_wrapper_without_harness_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_unknown_harness_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "bogus", "--", "bogus"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_missing_dashdash_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "claude", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_no_mode_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


# --- build-script mode ---

_CLAUDE_CONF = {
    "harness": "claude",
    "slug": "claude-deepseek-auto",
    "secretEnv": "DEEPSEEK_API_KEY",
    "config": {"baseUrl": "https://api.deepseek.com/anthropic", "model": "deepseek-v4-pro"},
}


def test_build_script_to_stdout(monkeypatch, tmp_path, capsys):
    conf = tmp_path / "claude.json"
    conf.write_text(json.dumps(_CLAUDE_CONF))
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script", str(conf)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("#!/usr/bin/env bash")
    assert "exec agedum --wrapper claude -- claude" in out


def test_build_script_to_file_is_executable(monkeypatch, tmp_path):
    conf = tmp_path / "claude.json"
    conf.write_text(json.dumps(_CLAUDE_CONF))
    out = tmp_path / "claude.sh"
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script", str(conf), str(out)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert out.read_text().startswith("#!/usr/bin/env bash")
    assert out.stat().st_mode & 0o111  # executable bit set


def test_build_script_check_passes_when_fresh(monkeypatch, tmp_path):
    conf = tmp_path / "claude.json"
    conf.write_text(json.dumps(_CLAUDE_CONF))
    out = tmp_path / "claude.sh"
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script", str(conf), str(out)])
    with pytest.raises(SystemExit):
        cli.app()
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script", "--check", str(conf), str(out)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0


def test_build_script_check_fails_when_stale(monkeypatch, tmp_path):
    conf = tmp_path / "claude.json"
    conf.write_text(json.dumps(_CLAUDE_CONF))
    out = tmp_path / "claude.sh"
    out.write_text("#!/usr/bin/env bash\n# stale\n")
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script", "--check", str(conf), str(out)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 1


def test_build_script_invalid_json_errors(monkeypatch, tmp_path):
    conf = tmp_path / "bad.json"
    conf.write_text("{ not json")
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script", str(conf)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 1


def test_build_script_requires_a_path(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--build-script"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2
