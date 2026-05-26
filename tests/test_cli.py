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
    assert "--claude" in out and "--kimi" in out


def test_claude_passes_everything_after_dashdash(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--claude", "--", "claude", "--model", "x", "-p"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "--model", "x", "-p"]


def test_kimi_mode_recognised(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--kimi", "--", "kimi", "-p", "hi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "hi"]


def test_missing_dashdash_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--claude", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_unknown_flag_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--bogus", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_no_mode_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2
