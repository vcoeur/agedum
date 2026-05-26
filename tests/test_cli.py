from typer.testing import CliRunner

from agedum import __version__
from agedum.cli.main import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_run_is_a_recognised_option() -> None:
    result = runner.invoke(app, ["--run", "do a thing"])
    assert result.exit_code == 0
    assert "do a thing" in result.stdout


def test_bare_invocation_launches() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "launch" in result.stdout.lower()
