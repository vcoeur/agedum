"""``agedum`` CLI entrypoint.

Shape: ``agedum <context-flags> -- <full command incl. binary>``.

The context flag before ``--`` chooses which virtual files to build (``--claude``
or ``--kimi``); everything after ``--`` is the child argv, run inside a mount
namespace where those files are injected at the harness's expected paths (some
harnesses also need extra flags appended — e.g. kimi's ``--agent-file``).
Decoupling context from command keeps the flag space open for more ``--<harness>``
modes / variants.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from agedum import __version__
from agedum.harness import Plan, compile_claude, compile_kimi, compile_opencode
from agedum.launcher import LauncherError, run_virtualfs
from agedum.sources import Source, load_global_source, load_source

_err = Console(stderr=True)

# context flag -> compiler
_COMPILERS: dict[str, Callable[[Source, Source | None, Path], Plan]] = {
    "claude": compile_claude,
    "kimi": compile_kimi,
    "opencode": compile_opencode,
}

USAGE = "usage: agedum (--claude | --kimi | --opencode) -- <command> [args...]"
HELP = f"""{USAGE}

Run a command inside a virtual-file context built from the project's
agent-neutral source (AGENTS.md + .agents/skills/), plus the global source
(~/.config/agents/AGENTS.md + ~/.agents/skills/).

Context flags (before --):
  --claude        build Claude-format virtual files for the command
  --kimi          build kimi-cli-format virtual files for the command
  --opencode      build opencode-format virtual files for the command

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
        name = flag.removeprefix("--")
        if name in _COMPILERS:
            mode = name
        else:
            _die(f"unknown option: {flag}")
    if mode is None:
        _die("a context mode is required: --claude, --kimi, or --opencode")

    raise SystemExit(_run(mode, command))


def _run(mode: str, command: list[str]) -> int:
    project = load_source()
    global_ = load_global_source()
    if not any((project.agents_md, project.skills_dir, global_.agents_md, global_.skills_dir)):
        _err.print(
            "[yellow]agedum:[/] no AGENTS.md or .agents/skills/ (project or global) — "
            "running the command with no injected context."
        )
    dest = Path(tempfile.mkdtemp(prefix=f"agedum-{mode}-"))
    try:
        plan = _COMPILERS[mode](project, global_, dest)
        return run_virtualfs(project.root, plan, command)
    except LauncherError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1
    finally:
        shutil.rmtree(dest, ignore_errors=True)
