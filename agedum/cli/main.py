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
``--`` chooses which virtual files to build (Claude / kimi / opencode / cline); everything after
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
from agedum.harness import Plan, compile_claude, compile_cline, compile_kimi, compile_opencode
from agedum.launcher import LauncherError, run_virtualfs
from agedum.provider import (
    ProviderError,
    build_launch,
    default_env_file,
    load_config,
    parse_env_file,
    resolve_config_path,
    with_prompt,
)
from agedum.sources import Source, load_global_source, load_source

_err = Console(stderr=True)

# harness name -> compiler
_COMPILERS: dict[str, Callable[[Source, Source | None, Path], Plan]] = {
    "claude": compile_claude,
    "kimi": compile_kimi,
    "opencode": compile_opencode,
    "cline": compile_cline,
}

USAGE = (
    "usage: agedum <provider-name|config.json> [--env <file>] [--prompt TEXT | --run TEXT] "
    "[--dry-run] [harness args...]\n"
    "       agedum --wrapper <claude|kimi|opencode|cline> [--dry-run] -- <command> [args...]"
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
                        would be injected, and the argv — don't launch (accepted before
                        or after the provider)
  --prompt TEXT         seed the harness with an initial prompt, then stay interactive
  --run TEXT            run the prompt non-interactively, then exit (no interactive UI);
                        --prompt and --run are mutually exclusive
  harness args          any token after the provider that isn't an agedum flag is passed
                        to the harness; use `--` to forward a literal --dry-run/--env

Wrapper mode (low-level; provider mode builds on it) — run any command inside the
virtual-file context, with no provider env:

  --wrapper <harness>   build virtual files for the harness (claude | kimi | opencode |
                        cline) then run the command after -- inside the namespace
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
    prompt_text: str | None = None
    prompt_interactive = False

    def claim_prompt(interactive: bool, value: str) -> None:
        nonlocal prompt_text, prompt_interactive
        if prompt_text is not None:
            _die("--prompt and --run are mutually exclusive and may be given only once")
        prompt_text = value
        prompt_interactive = interactive

    # agedum's own flags (--env, --dry-run) are recognised wherever they appear before
    # an explicit `--`, including after the provider name — so the documented
    # `agedum <provider> --dry-run` works. The provider is the first bare token; any
    # other token after it is harness passthrough. A `--` once the provider is known
    # ends agedum parsing: everything past it goes to the harness verbatim, the escape
    # hatch for forwarding a literal --dry-run/--env to the harness.
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            if provider is None:
                # Disambiguating separator: the next token is the provider name.
                index += 1
                if index >= len(argv):
                    _die("missing provider after `--`")
                provider = argv[index]
            else:
                rest.extend(argv[index + 1 :])
                break
        elif arg.startswith("--env="):
            env_file = arg.split("=", 1)[1]
        elif arg == "--env":
            index += 1
            if index >= len(argv):
                _die("--env requires a file path")
            env_file = argv[index]
        elif arg == "--dry-run":
            dry_run = True
        elif arg.startswith("--prompt=") or arg.startswith("--run="):
            flag, value = arg.split("=", 1)
            claim_prompt(flag == "--prompt", value)
        elif arg in ("--prompt", "--run"):
            index += 1
            if index >= len(argv):
                _die(f"{arg} requires a prompt string")
            claim_prompt(arg == "--prompt", argv[index])
        elif provider is None and arg.startswith("-") and arg != "-":
            _die(f"unknown option: {arg}")
        elif provider is None:
            provider = arg
        else:
            rest.append(arg)
        index += 1

    if provider is None:
        _die("a provider name or config path is required")

    try:
        config = load_config(resolve_config_path(provider))
        env_path = Path(env_file).expanduser() if env_file else default_env_file()
        dotenv = parse_env_file(env_path) if env_path.is_file() else {}
        launch = build_launch(config, {**os.environ, **dotenv})
        command = (
            with_prompt(launch, rest, prompt_text, interactive=prompt_interactive)
            if prompt_text is not None
            else [*launch.command, *rest]
        )
    except ProviderError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1

    if dry_run:
        _print_dry_run(launch, env_path, command)
        return 0

    os.environ.update(launch.env)
    for var in launch.unset:
        os.environ.pop(var, None)
    # `--run` is non-interactive: the prompt is in argv, so the harness must not inherit a
    # live stdin to block on (see run_virtualfs). `--prompt` and a bare launch stay interactive.
    non_interactive = prompt_text is not None and not prompt_interactive
    return _run(launch.harness, command, close_stdin=non_interactive)


def _print_dry_run(launch, env_path: Path, command: list[str]) -> None:
    """Print the resolved launch (secrets masked) without running anything."""
    print(f"provider   {launch.label}")
    print(f"harness    {launch.harness}")
    print(f"env file   {_abs_display(env_path)}")
    print()
    _print_environment(launch)
    extra_args = _print_plan_sections(launch.harness)
    _print_command(command, extra_args, _secret_values(launch))


def _secret_values(launch) -> list[str]:
    """Secret env-var values to redact from ``--dry-run`` output, longest first so a
    shorter secret that is a substring of a longer one cannot leave a fragment unmasked.

    Used to scrub both the pretty-printed opencode config (a key baked into
    ``OPENCODE_CONFIG_CONTENT`` — excluded here, redacted in place) and the launched
    command (cline passes the token as a ``--key`` argv flag)."""
    return sorted(
        (
            launch.env[name]
            for name in launch.secrets
            if name != "OPENCODE_CONFIG_CONTENT" and launch.env.get(name)
        ),
        key=len,
        reverse=True,
    )


def _print_environment(launch) -> None:
    """The resolved provider environment (secrets masked; opencode config pretty-printed)."""
    if not launch.env and not launch.unset:
        return
    print("environment")
    width = max((len(key) for key in launch.env), default=0)
    secret_values = _secret_values(launch)
    for key, value in launch.env.items():
        if key == "OPENCODE_CONFIG_CONTENT":
            # The resolved opencode config — pretty-print the JSON instead of a one-liner.
            print(f"  {key}")
            for line in _redact(_pretty_json(value), secret_values).splitlines():
                print(f"    {line}")
        else:
            print(f"  {key.ljust(width)}   {'***' if key in launch.secrets else value}")
    for var in launch.unset:
        print(f"  unset {var}")
    print()


def _print_command(
    command: list[str], extra_args: list[str], secret_values: list[str] = ()
) -> None:
    print("command")
    print(f"  {_redact(' '.join(command), secret_values)}")
    if extra_args:
        print(f"  + agedum appends: {_redact(' '.join(extra_args), secret_values)}")


def _redact(text: str, secret_values: list[str]) -> str:
    """Replace each secret value with ``***`` (longest first, so a shorter secret that
    is a substring of a longer one cannot leave a fragment unmasked)."""
    for secret in secret_values:
        text = text.replace(secret, "***")
    return text


def _pretty_json(text: str) -> str:
    """Re-render a compact JSON string indented; pass it through unchanged if unparseable."""
    try:
        return json.dumps(json.loads(text), indent=2, sort_keys=True)
    except json.JSONDecodeError:
        return text


def _abs_display(path: Path) -> str:
    """Absolute path with the user's home abbreviated to ``~``."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home) else text


def _display_path(path: Path) -> str:
    """Render a path relative to the cwd when it lives under it, else home-abbreviated.

    Paths inside the current directory read as ``.agents/skills`` etc.; anything else
    (the global config, a parent project) keeps a ``~``-abbreviated absolute form.
    """
    rel = os.path.relpath(path, Path.cwd())
    if not rel.startswith(".."):
        return rel
    return _abs_display(path)


def _print_plan_sections(mode: str) -> list[str]:
    """Compile the located sources and print the per-scope source dispositions.

    For each scope (project, global) every source is listed with what happens to it:
    injected ``→ <dest>``, ``read in place`` (kimi/opencode read the project AGENTS.md
    natively), routed to the kimi ``--agent-file``, or an explicit note when the scope is
    empty. Returns the launch's appended args (kimi ``--agent-file``) for the command line.
    """
    project = load_source()
    global_ = load_global_source()
    dest = Path(tempfile.mkdtemp(prefix=f"agedum-{mode}-dry-"))
    try:
        plan = _COMPILERS[mode](project, global_, dest)
        # is_dir() reflects whether a bind is a skills dir vs a file; resolve before cleanup.
        dir_targets = {target for src, target in plan.binds if src.is_dir()}
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    # Project-scope paths display relative to the cwd; global-scope stays ~-absolute so
    # the user config never looks like a project-relative path (e.g. when run from $HOME).
    _print_scope(
        f"project scope · {_abs_display(project.root)}",
        project,
        plan,
        dir_targets,
        empty="(no AGENTS.md or .agents/skills found here)",
        display=_display_path,
    )
    _print_scope(
        "global scope",
        global_,
        plan,
        dir_targets,
        empty="(no ~/.config/agents/AGENTS.md or ~/.config/agents/skills)",
        display=_abs_display,
    )
    return plan.extra_args


def _print_scope(
    title: str, source: Source, plan: Plan, dir_targets: set, *, empty: str, display
) -> None:
    """Print one scope's sources, each with its disposition, aligned."""
    print(title)
    rows = []
    for path in (source.agents_md, source.skills_dir):
        if path is None:
            continue
        label = f"{display(path)}{'/' if path.is_dir() else ''}"
        rows.append((label, _disposition(path, plan, dir_targets, display)))
    if not rows:
        print(f"  {empty}")
    else:
        width = max(len(label) for label, _ in rows)
        for label, disposition in rows:
            print(f"  {label.ljust(width)}   {disposition}")
    print()


def _disposition(source_path: Path, plan: Plan, dir_targets: set, display) -> str:
    """How the harness consumes ``source_path``: injected, native, agent-file, or neither."""
    key = str(source_path)
    for _, target in plan.binds:
        if plan.origins.get(target) == key:
            return f"→ {display(target)}{'/' if target in dir_targets else ''}"
    for token in plan.extra_args:
        if plan.origins.get(Path(token)) == key:
            return "→ kimi agent file (passed via --agent-file)"
    if source_path in plan.native_reads:
        return "read in place (read natively — not injected)"
    return "(not injected)"


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
                    _die("--wrapper requires a harness: claude, kimi, opencode, or cline")
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
        _die("a harness is required: --wrapper claude|kimi|opencode|cline")

    if dry_run:
        print(f"harness    {mode}")
        print()
        extra_args = _print_plan_sections(mode)
        _print_command(command, extra_args)
        return 0

    return _run(mode, command)


def _run(mode: str, command: list[str], *, close_stdin: bool = False) -> int:
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
            return run_virtualfs(project.root, plan, command, close_stdin=close_stdin)
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
