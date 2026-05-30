"""``agedum`` CLI entrypoint — two modes.

**provider** (the primary form): ``agedum <name|path> [harness args…]``. Reads a
condash-style provider config (a name resolved under
``${AGENTS_PROVIDERS_DIR:-~/.config/agents/providers}`` or a ``/``/``.json`` path),
resolves its env from ``${AGENTS_ENV_FILE:-~/.config/agents/.env}`` (or ``--env``),
sets the provider/model/auth environment, and launches the harness named in the config
inside the virtual-file context. ``--dry-run`` prints the resolved env (secrets masked),
the virtual files that would be injected, and the final argv without launching. See
:mod:`agedum.provider`.

**wrapper**: ``agedum --wrapper <harness> [--dry-run] -- <command…>``. The harness before
``--`` chooses which virtual files to build (Claude / kimi / opencode); everything after
``--`` is the child argv, run inside a mount namespace where those files are injected at
the harness's expected paths. ``--dry-run`` prints the virtual files that would be
injected without running the command. This is the low-level entry that provider mode
builds on; users normally launch via ``agedum <name>``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console

from agedum import __version__
from agedum.harness import Plan, compile_claude, compile_kimi, compile_opencode
from agedum.launcher import LauncherError, run_virtualfs
from agedum.provider import (
    ProviderError,
    build_launch,
    default_env_file,
    load_config,
    parse_env_file,
    resolve_config_path,
)
from agedum.sources import Source, load_global_source, load_source

_err = Console(stderr=True)

# harness name -> compiler
_COMPILERS: dict[str, Callable[[Source, Source | None, Path], Plan]] = {
    "claude": compile_claude,
    "kimi": compile_kimi,
    "opencode": compile_opencode,
}

USAGE = (
    "usage: agedum <provider-name|config.json> [--env <file>] [--dry-run] [harness args...]\n"
    "       agedum --wrapper <claude|kimi|opencode> [--dry-run] -- <command> [args...]"
)
HELP = f"""{USAGE}

Provider mode (the normal way to launch) — run a harness from a provider config JSON,
with the provider's env resolved from the env file and the agent-neutral source injected
as virtual files:

  <provider-name>       resolve <name>.json under $AGENTS_PROVIDERS_DIR
                        (default ~/.config/agents/providers)
  <config.json> / path  a config path (contains / or ends in .json; CWD-relative)
  --env <file>          override the env file ($AGENTS_ENV_FILE, default
                        ~/.config/agents/.env)
  --dry-run             print the resolved env (secrets masked), the virtual files that
                        would be injected, and the argv — don't launch
  harness args          everything after the provider is passed to the harness

Wrapper mode (low-level; provider mode builds on it) — run any command inside the
virtual-file context, with no provider env:

  --wrapper <harness>   build virtual files for the harness (claude | kimi | opencode)
                        then run the command after -- inside the namespace
  --dry-run             print the virtual files that would be injected, don't run

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

    first = argv[0]
    is_wrapper = first == "--wrapper" or first.startswith("--wrapper=")
    if is_wrapper:
        raise SystemExit(_run_wrapper(argv))
    raise SystemExit(_run_config(argv))


# ---------------------------------------------------------------------------
# provider mode
# ---------------------------------------------------------------------------


def _run_config(argv: list[str]) -> int:
    env_file: str | None = None
    dry_run = False
    provider: str | None = None
    rest: list[str] = []

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--env="):
            env_file = arg.split("=", 1)[1]
        elif arg == "--env":
            index += 1
            if index >= len(argv):
                _die("--env requires a file path")
            env_file = argv[index]
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--":
            index += 1
            if index >= len(argv):
                _die("missing provider after `--`")
            provider = argv[index]
            rest = argv[index + 1 :]
            break
        elif arg.startswith("-") and arg != "-":
            _die(f"unknown option: {arg}")
        else:
            provider = arg
            rest = argv[index + 1 :]
            break
        index += 1

    if provider is None:
        _die("a provider name or config path is required")

    try:
        config = load_config(resolve_config_path(provider))
        env_path = Path(env_file).expanduser() if env_file else default_env_file()
        dotenv = parse_env_file(env_path) if env_path.is_file() else {}
        launch = build_launch(config, {**os.environ, **dotenv})
    except ProviderError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1

    command = [*launch.command, *rest]
    if dry_run:
        _print_dry_run(launch, env_path, command)
        return 0

    os.environ.update(launch.env)
    for var in launch.unset:
        os.environ.pop(var, None)
    return _run(launch.harness, command)


def _print_dry_run(launch, env_path: Path, command: list[str]) -> None:
    """Print the resolved launch (secret values masked) without running anything."""
    print(f"provider: {launch.label}")
    print(f"harness:  {launch.harness}")
    print(f"env file: {env_path}")
    for key, value in launch.env.items():
        if key in launch.secrets:
            print(f"  export {key}=***")
        elif key == "OPENCODE_CONFIG_CONTENT":
            # The resolved opencode config — pretty-print the JSON instead of a one-liner.
            print(f"  export {key}=")
            for line in _pretty_json(value).splitlines():
                print(f"    {line}")
        else:
            print(f"  export {key}={value}")
    for var in launch.unset:
        print(f"  unset {var}")
    print(f"command:  {' '.join(command)}")
    print(f"virtual files ({launch.harness}):")
    for line in _virtual_file_lines(launch.harness):
        print(line)


def _pretty_json(text: str) -> str:
    """Re-render a compact JSON string indented; pass it through unchanged if unparseable."""
    try:
        return json.dumps(json.loads(text), indent=2, sort_keys=True)
    except json.JSONDecodeError:
        return text


def _display_path(path: Path) -> str:
    """Render a path with the user's home abbreviated to ``~``."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home) else text


def _virtual_file_lines(mode: str) -> list[str]:
    """Describe the virtual files agedum would inject for ``mode``, without launching.

    Compiles the located project + global sources to a throwaway directory to obtain the
    bind plan, then formats each injection as ``<source> → <dest>`` — the agent-neutral
    source file (from ``plan.origins``) and the path the harness will read, directories
    marked with a trailing ``/``. kimi's global ``AGENTS.md`` (injected via
    ``--agent-file``) is listed too. The throwaway directory is removed before returning.
    """
    project = load_source()
    global_ = load_global_source()
    if not any((project.agents_md, project.skills_dir, global_.agents_md, global_.skills_dir)):
        return ["  (no AGENTS.md or skills found — nothing injected)"]
    dest = Path(tempfile.mkdtemp(prefix=f"agedum-{mode}-dry-"))
    try:
        plan = _COMPILERS[mode](project, global_, dest)
        lines = []
        for src, target in plan.binds:
            lines.append(_injection_line(plan.origins.get(target), target, is_dir=src.is_dir()))
        # kimi injects the global AGENTS.md via --agent-file rather than a bind: it is
        # compiled into a generated agent file that kimi reads through that flag.
        for token in plan.extra_args:
            origin = plan.origins.get(Path(token))
            if origin:
                lines.append(
                    f"  {_display_path(Path(origin))} → kimi agent file (passed via --agent-file)"
                )
        if not lines:
            lines.append("  (none)")
        if plan.extra_args:
            lines.append(f"  appended args: {' '.join(plan.extra_args)}")
        return lines
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _injection_line(origin: str | None, target: Path, *, is_dir: bool) -> str:
    """Format one ``<source> → <dest>`` line (dest dirs get a trailing ``/``)."""
    dest = f"{_display_path(target)}{'/' if is_dir else ''}"
    if origin is None:
        return f"  {dest}"
    source = f"{_display_path(Path(origin))}{'/' if Path(origin).is_dir() else ''}"
    return f"  {source} → {dest}"


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
    dry_run = False
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
        elif flag == "--dry-run":
            dry_run = True
        else:
            _die(f"unknown option: {flag}")
        index += 1

    if mode is None:
        _die("a harness is required: --wrapper claude|kimi|opencode")

    if dry_run:
        print(f"harness:  {mode}")
        print(f"command:  {' '.join(command)}")
        print(f"virtual files ({mode}):")
        for line in _virtual_file_lines(mode):
            print(line)
        return 0

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
        with _maybe_fold_proxy(mode):
            return run_virtualfs(project.root, plan, command)
    except LauncherError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1
    finally:
        shutil.rmtree(dest, ignore_errors=True)


@contextmanager
def _maybe_fold_proxy(mode: str) -> Iterator[None]:
    """Interpose the system-role fold proxy when the provider opted in.

    Enabled by ``AGEDUM_FOLD_SYSTEM_MESSAGES=1`` (set by a provider config's
    ``foldSystemMessages`` flag) for the claude harness. The child's
    ``ANTHROPIC_BASE_URL`` is repointed at a local proxy that folds ``system``-role
    messages into the top-level ``system`` field before forwarding to the real upstream.
    A no-op otherwise.
    """
    upstream = os.environ.get("ANTHROPIC_BASE_URL", "")
    if mode != "claude" or os.environ.get("AGEDUM_FOLD_SYSTEM_MESSAGES") != "1" or not upstream:
        yield
        return

    from agedum.proxy import FoldProxy

    with FoldProxy(upstream) as proxy:
        os.environ["ANTHROPIC_BASE_URL"] = proxy.base_url
        try:
            yield
        finally:
            os.environ["ANTHROPIC_BASE_URL"] = upstream
