"""Typer CLI entrypoint for the ``agedum`` command.

agedum is invoked as the agent binary: bare (``agedum``) to launch interactively,
or ``agedum --run "<PROMPT>"`` to execute a one-shot task — the contract condash's
``agentsconf`` harness drives. The launch/translate pipeline (resolve the
``AGENTS.md`` + ``.agents/skills`` source, render per harness, exec the CLI) is not
implemented yet; the commands below are scaffolding.
"""

from __future__ import annotations

import typer
from rich.console import Console

from agedum import __version__

app = typer.Typer(
    name="agedum",
    help="Drive an agent CLI from the AGENTS.md + .agents/skills source shape.",
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agedum {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    run: str | None = typer.Option(
        None,
        "--run",
        metavar="PROMPT",
        help='Run a one-shot task non-interactively (e.g. agedum --run "fix the bug").',
    ),
) -> None:
    """Launch the configured agent. With --run, execute a one-shot task."""
    if ctx.invoked_subcommand is not None:
        return
    # Scaffold: the resolve/translate/exec pipeline is not implemented yet.
    if run is not None:
        console.print(f"[yellow]agedum[/] (scaffold): would run task: {run!r}")
    else:
        console.print("[yellow]agedum[/] (scaffold): would launch interactively")
    raise typer.Exit()
