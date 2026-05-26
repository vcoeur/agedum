"""``agedum`` CLI entrypoint.

Shape: ``agedum <context-flags> -- <full command incl. binary>``.

The context flags before ``--`` choose which virtual files to build (``--claude``
for Claude format); everything after ``--`` is the child argv, run verbatim inside
a mount namespace where those files are injected at the harness's expected paths.
Decoupling context from command means one context can front any command, and the
flag space stays open for future ``--<harness>`` modes / variants.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from agedum import __version__
from agedum.harness import compile_claude
from agedum.launcher import LauncherError, run_virtualfs
from agedum.sources import load_source

_err = Console(stderr=True)

USAGE = "usage: agedum --claude -- <command> [args...]"
HELP = f"""{USAGE}

Run a command inside a virtual-file context built from the project's
agent-neutral source (AGENTS.md + .agents/skills/).

Context flags (before --):
  --claude        build Claude-format virtual files for the command

Other:
  --version       print the version and exit
  -h, --help      show this help
"""


def _die(message: str, code: int = 2) -> None:
    _err.print(f"[red]agedum:[/] {message}")
    _err.print(USAGE)
    raise SystemExit(code)


def app() -> None:
    """Console-script entrypoint (``agedum``)."""
    argv = sys.argv[1:]

    if argv and argv[0] in ("--version", "-V"):
        print(f"agedum {__version__}")
        return
    if not argv or argv[0] in ("-h", "--help"):
        print(HELP)
        return

    if "--" not in argv:
        _die("missing `--` separator before the command")
    split = argv.index("--")
    flags, command = argv[:split], argv[split + 1 :]
    if not command:
        _die("no command given after `--`")

    mode: str | None = None
    for flag in flags:
        if flag == "--claude":
            mode = "claude"
        else:
            _die(f"unknown option: {flag}")
    if mode is None:
        _die("a context mode is required (currently: --claude)")

    raise SystemExit(_run_claude(command))


def _run_claude(command: list[str]) -> int:
    source = load_source()
    if source.agents_md is None and source.skills_dir is None:
        _err.print(
            "[yellow]agedum:[/] no AGENTS.md or .agents/skills/ found under the project "
            "root — running the command with no injected context."
        )
    dest = Path(tempfile.mkdtemp(prefix="agedum-claude-"))
    try:
        plan = compile_claude(source, dest)
        return run_virtualfs(source.root, plan, command)
    except LauncherError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1
    finally:
        shutil.rmtree(dest, ignore_errors=True)
