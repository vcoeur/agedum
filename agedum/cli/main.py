"""``agedum`` CLI entrypoint — two modes.

**wrapper** (run time): ``agedum --wrapper <harness> -- <command…>``. The harness
before ``--`` chooses which virtual files to build (Claude / kimi / opencode);
everything after ``--`` is the child argv, run inside a mount namespace where those
files are injected at the harness's expected paths. The legacy ``--claude`` /
``--kimi`` / ``--opencode`` flags are kept as deprecated aliases for ``--wrapper <h>``.

**build-script** (compile time): ``agedum --build-script [--check] conf.json [out.sh]``.
Compile a condash-style provider config JSON into a standalone shell wrapper that sets
the provider/model/auth environment then ``exec``s ``agedum --wrapper`` — see
:mod:`agedum.buildscript`.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from agedum import __version__
from agedum.buildscript import BuildScriptError, build_script_from_file
from agedum.harness import Plan, compile_claude, compile_kimi, compile_opencode
from agedum.launcher import LauncherError, run_virtualfs
from agedum.sources import Source, load_global_source, load_source

_err = Console(stderr=True)

# harness name -> compiler
_COMPILERS: dict[str, Callable[[Source, Source | None, Path], Plan]] = {
    "claude": compile_claude,
    "kimi": compile_kimi,
    "opencode": compile_opencode,
}

USAGE = (
    "usage: agedum --wrapper <claude|kimi|opencode> -- <command> [args...]\n"
    "       agedum --build-script [--check] <config.json> [output.sh]"
)
HELP = f"""{USAGE}

Wrapper mode — run a command inside a virtual-file context built from the project's
agent-neutral source (AGENTS.md + .agents/skills/) plus the global source
(~/.config/agents/AGENTS.md + ~/.agents/skills/):

  --wrapper <harness>   build virtual files for the harness (claude | kimi | opencode)
                        then run the command after -- inside the namespace
  --claude / --kimi / --opencode
                        deprecated aliases for `--wrapper <harness>`

Build-script mode — compile a provider config JSON into a shell wrapper that sets the
provider/model/auth environment and execs `agedum --wrapper <harness> -- <harness>`:

  --build-script <config.json> [output.sh]   write the wrapper (stdout if no output)
  --check                                     with an output.sh, exit non-zero if it
                                              is stale vs a fresh generation

Other:
  --version, -V         print the version and exit
  -h, --help            show this help
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

    if argv[0] == "--build-script":
        raise SystemExit(_run_build_script(argv[1:]))

    raise SystemExit(_run_wrapper(argv))


# ---------------------------------------------------------------------------
# wrapper mode
# ---------------------------------------------------------------------------


def _run_wrapper(argv: list[str]) -> int:
    if "--" not in argv:
        _die("missing `--` separator before the command")
    split = argv.index("--")
    flags, command = argv[:split], argv[split + 1 :]
    if not command:
        _die("no command given after `--`")

    mode: str | None = None
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag == "--wrapper" or flag.startswith("--wrapper="):
            if "=" in flag:
                value = flag.split("=", 1)[1]
            else:
                index += 1
                if index >= len(flags):
                    _die("--wrapper requires a harness: claude, kimi, or opencode")
                value = flags[index]
            if value not in _COMPILERS:
                _die(f"unknown harness for --wrapper: {value}")
            mode = value
        elif flag.removeprefix("--") in _COMPILERS:
            name = flag.removeprefix("--")
            _err.print(f"[yellow]agedum:[/] `{flag}` is deprecated; use `--wrapper {name}`")
            mode = name
        else:
            _die(f"unknown option: {flag}")
        index += 1

    if mode is None:
        _die("a harness is required: --wrapper claude|kimi|opencode")

    return _run(mode, command)


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


# ---------------------------------------------------------------------------
# build-script mode
# ---------------------------------------------------------------------------


def _run_build_script(args: list[str]) -> int:
    check = False
    positionals: list[str] = []
    for arg in args:
        if arg == "--check":
            check = True
        elif arg.startswith("-") and arg != "-":
            _die(f"unknown option for --build-script: {arg}")
        else:
            positionals.append(arg)

    if not positionals:
        _die("--build-script requires a config JSON path")
    if len(positionals) > 2:
        _die("--build-script takes at most a config path and an output path")
    config_path = Path(positionals[0])
    output_path = Path(positionals[1]) if len(positionals) > 1 else None

    try:
        script = build_script_from_file(config_path)
    except BuildScriptError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1

    if check:
        if output_path is None:
            _die("--check requires an output.sh path to compare against")
        if not output_path.is_file():
            _err.print(f"[red]agedum:[/] {output_path} does not exist — generate it first")
            return 1
        if output_path.read_text() != script:
            _err.print(f"[red]agedum:[/] {output_path} is stale — regenerate it from {config_path}")
            return 1
        return 0

    if output_path is None:
        sys.stdout.write(script)
    else:
        output_path.write_text(script)
        output_path.chmod(0o755)
    return 0
