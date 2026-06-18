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
``--`` chooses which virtual files to build (Claude / kimi / opencode / cline / reasonix /
aider / pi); everything after ``--`` is the child argv, run inside a mount namespace where
those files are injected at
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
from agedum.harness import (
    Plan,
    Sandbox,
    compile_aider,
    compile_claude,
    compile_cline,
    compile_codex,
    compile_kimi,
    compile_opencode,
    compile_pi,
    compile_reasonix,
)
from agedum.launcher import LauncherError, run_virtualfs, writable_roots
from agedum.provider import (
    CODEX_PROVIDER_NAME,
    ProviderError,
    build_launch,
    default_env_file,
    list_providers,
    load_config,
    load_merged_config,
    merge_json_onto_file,
    parse_env_file,
    providers_dir,
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
    "reasonix": compile_reasonix,
    "aider": compile_aider,
    "pi": compile_pi,
    "codex": compile_codex,
}

USAGE = (
    "usage: agedum <provider-name|config.json> [--env <file>] [--prompt TEXT | --run TEXT] "
    "[--dry-run] [harness args...]\n"
    "       agedum --wrapper <claude|kimi|opencode|cline|reasonix|aider|pi|codex> [--sandbox] "
    "[--rw-dir DIR]... [--dry-run] -- <command> [args...]"
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
                        cline | reasonix | aider | pi | codex) then run the command after --
                        inside the namespace
  --sandbox             write-confinement: mount the host read-only so the command can only
                        write the project root, agedum's injection dirs, /tmp, and any
                        --rw-dir paths (default: full read-write host access)
  --rw-dir DIR          add DIR to the writable set (repeatable); implies --sandbox
  --dry-run             print the virtual files that would be injected, don't run

Other:
  --providers           list the provider configs in $AGENTS_PROVIDERS_DIR
                        (default ~/.config/agents/providers) and exit
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
    if argv and argv[0] == "--providers":
        raise SystemExit(_run_list_providers())
    if not argv or argv[0] in ("-h", "--help"):
        print(HELP)
        return

    first = argv[0]
    is_wrapper = first == "--wrapper" or first.startswith("--wrapper=")
    if is_wrapper:
        raise SystemExit(_run_wrapper(argv))
    raise SystemExit(_run_config(argv))


def _run_list_providers() -> int:
    """Print every provider config in the providers dir as ``name  harness  model``, then exit.

    Honours ``$AGENTS_PROVIDERS_DIR`` (default ``~/.config/agents/providers``). A missing or
    empty directory is stated rather than left as a bare header; a config that won't parse is
    listed with its error so it stays visible instead of being silently dropped.
    """
    directory = providers_dir()
    summaries = list_providers(directory)
    print(f"providers in {_abs_display(directory)}")
    if not directory.is_dir():
        _err.print("[yellow]agedum:[/] no providers directory there")
        return 0
    if not summaries:
        print("  (none)")
        return 0
    print()
    name_width = max(len(summary.name) for summary in summaries)
    harness_width = max(len(summary.harness or "?") for summary in summaries)
    for summary in summaries:
        if summary.error is not None:
            print(f"  {summary.name.ljust(name_width)}   [unreadable: {summary.error}]")
            continue
        harness = (summary.harness or "?").ljust(harness_width)
        print(f"  {summary.name.ljust(name_width)}   {harness}   {summary.model or '-'}")
    return 0


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

    env_path = Path(env_file).expanduser() if env_file else default_env_file()
    if env_file is not None and not env_path.is_file():
        # An *explicit* --env that doesn't resolve is a user error — failing loudly here
        # beats the misleading "X is required by provider … but is not set" downstream.
        # The default env file stays optional (running without a .env is normal).
        _die(f"--env file not found: {env_path}")

    try:
        config_path = resolve_config_path(provider)
        if load_config(config_path).get("abstract") is True:
            raise ProviderError(
                f"{provider} is an abstract base config (abstract: true) and cannot be "
                "launched directly — launch a config that extends it"
            )
        config = load_merged_config(config_path)
        dotenv = parse_env_file(env_path) if env_path.is_file() else {}
        launch = build_launch(config, {**os.environ, **dotenv}, label=provider)
        command = (
            with_prompt(launch, rest, prompt_text, interactive=prompt_interactive)
            if prompt_text is not None
            else [*launch.command, *rest]
        )
    except ProviderError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1

    for warning in launch.warnings:
        _err.print(f"[yellow]agedum:[/] {warning}")

    if dry_run:
        _print_dry_run(launch, env_path, command)
        return 0

    os.environ.update(launch.env)
    for var in launch.unset:
        os.environ.pop(var, None)
    # `--run` is non-interactive: the prompt is in argv, so the harness must not inherit a
    # live stdin to block on (see run_virtualfs). `--prompt` and a bare launch stay interactive.
    non_interactive = prompt_text is not None and not prompt_interactive
    return _run(
        launch.harness,
        command,
        close_stdin=non_interactive,
        config_files=launch.config_files,
        sandbox=launch.sandbox,
    )


def _print_dry_run(launch, env_path: Path, command: list[str]) -> None:
    """Print the resolved launch (secrets masked) without running anything."""
    print(f"provider   {launch.label}")
    print(f"harness    {launch.harness}")
    print(f"env file   {_abs_display(env_path)}")
    print()
    _print_environment(launch)
    _print_proxy(launch)
    _print_config_files(launch)
    extra_args = _print_plan_sections(launch.harness, launch.sandbox)
    _print_command(command, extra_args, _secret_values(launch))


def _print_proxy(launch) -> None:
    """Show the local proxy interposed in front of the harness's endpoint, if any.

    The env table alone doesn't make it obvious that the harness will talk to a localhost
    address rather than the configured ``baseUrl``, so name the proxy and its real upstream.
    """
    codex_upstream = launch.env.get("AGEDUM_CODEX_CHAT_UPSTREAM")
    if codex_upstream:
        print("proxy")
        print(f"  responses→chat-completions → {codex_upstream}")
        print()
        return
    upstream = launch.env.get("ANTHROPIC_BASE_URL")
    if not upstream:
        return
    if launch.env.get("AGEDUM_TRANSLATE_OPENAI") == "1":
        print("proxy")
        print(f"  translate openai-completions → {upstream}")
        print()
    elif launch.env.get("AGEDUM_FOLD_SYSTEM_MESSAGES") == "1":
        print("proxy")
        print(f"  fold-system-messages → {upstream}")
        print()


def _print_config_files(launch) -> None:
    """Show any agedum-generated config files (reasonix's ``reasonix.toml``, pi's user-scope
    ``models.json`` / ``settings.json``, kimi's ``--config-file``).

    Each is bound inside the namespace at its target (project-root-relative or absolute). Most
    content references keys by env-var name, but some endpoints can't (kimi's config does not
    interpolate ``$ENV``, so its api_key is baked in verbatim), so secret values are redacted
    here just as they are in the environment and command output; a merged file shows the agedum
    fragment with a note.
    """
    if not launch.config_files:
        return
    secret_values = _secret_values(launch)
    print("generated config files")
    for target, content, merge_json in launch.config_files:
        note = "   (merged onto any existing file)" if merge_json else ""
        print(f"  {target}{note}")
        for line in _redact(content, secret_values).splitlines():
            print(f"    {line}")
    print()


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


def _print_plan_sections(mode: str, sandbox: Sandbox | None = None) -> list[str]:
    """Compile the located sources and print the per-scope source dispositions.

    For each scope (project, global) every source is listed with what happens to it:
    injected ``→ <dest>``, ``read in place`` (kimi/opencode read the project AGENTS.md
    natively), routed to the kimi ``--agent-file``, or an explicit note when the scope is
    empty. When ``sandbox`` is enabled, the write-confinement section (the read-write set
    over a read-only host) is printed too. Returns the launch's appended args (kimi
    ``--agent-file``) for the command line.
    """
    project = load_source()
    global_ = load_global_source()
    dest = Path(tempfile.mkdtemp(prefix=f"agedum-{mode}-dry-"))
    try:
        plan = _COMPILERS[mode](project, global_, dest)
        # is_dir() reflects whether a bind is a skills dir vs a file; resolve before cleanup.
        dir_targets = {target for src, target in plan.binds if src.is_dir()}
        # Resolve the writable set while the compiled sources still exist (rmtree below).
        sandbox_roots = (
            writable_roots(plan, sandbox, project.root)
            if sandbox is not None and sandbox.enabled
            else None
        )
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
    if sandbox_roots is not None:
        _print_sandbox(sandbox_roots)
    return plan.extra_args


def _print_sandbox(roots: list[Path]) -> None:
    """Print the write-confinement plan: the host is read-only; only these are writable."""
    print("sandbox · write-confinement (host mounted read-only)")
    rows = [*(f"{_abs_display(root)}/" for root in roots), "/tmp/  (private tmpfs scratch)"]
    for row in rows:
        print(f"  rw  {row}")
    print()


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
    """How the harness consumes ``source_path``: injected, native, an appended flag, or neither."""
    key = str(source_path)
    for _, target in plan.binds:
        if plan.origins.get(target) == key:
            return f"→ {display(target)}{'/' if target in dir_targets else ''}"
    for index, token in enumerate(plan.extra_args):
        if plan.origins.get(Path(token)) == key:
            # The flag preceding the matching token tells us how it is passed: kimi's
            # --agent-file vs aider's --read.
            flag = plan.extra_args[index - 1] if index > 0 else ""
            if flag == "--agent-file":
                return "→ kimi agent file (passed via --agent-file)"
            if flag == "--read":
                return "→ aider read-only context (passed via --read)"
            return f"→ passed via {flag}" if flag else "→ (appended arg)"
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
    sandbox_enabled = False
    rw_dirs: list[str] = []
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag == "--wrapper" or flag.startswith("--wrapper="):
            if "=" in flag:
                value = flag.split("=", 1)[1]
            else:
                index += 1
                if index >= len(flags):
                    _die(
                        "--wrapper requires a harness: "
                        "claude, kimi, opencode, cline, reasonix, aider, pi, or codex"
                    )
                value = flags[index]
            if value not in _COMPILERS:
                _die(f"unknown harness for --wrapper: {value}")
            mode = value
        elif flag == "--dry-run":
            dry_run = True
        elif flag == "--sandbox":
            sandbox_enabled = True
        elif flag == "--rw-dir" or flag.startswith("--rw-dir="):
            if "=" in flag:
                rw_dirs.append(flag.split("=", 1)[1])
            else:
                index += 1
                if index >= len(flags):
                    _die("--rw-dir requires a directory path")
                rw_dirs.append(flags[index])
        else:
            _die(f"unknown option: {flag}")
        index += 1

    if mode is None:
        _die("a harness is required: --wrapper claude|kimi|opencode|cline|reasonix|aider|pi|codex")

    # --rw-dir implies confinement (the paths are meaningless without a read-only host).
    sandbox = (
        Sandbox(enabled=True, read_write=tuple(rw_dirs)) if (sandbox_enabled or rw_dirs) else None
    )

    if dry_run:
        print(f"harness    {mode}")
        print()
        extra_args = _print_plan_sections(mode, sandbox)
        _print_command(command, extra_args)
        return 0

    return _run(mode, command, sandbox=sandbox)


def _run(
    mode: str,
    command: list[str],
    *,
    close_stdin: bool = False,
    config_files: tuple[tuple[str, str, bool], ...] = (),
    sandbox: Sandbox | None = None,
) -> int:
    project = load_source()
    global_ = load_global_source()
    has_context = any(
        (project.agents_md, project.skills_dir, global_.agents_md, global_.skills_dir)
    )
    if not has_context and not config_files:
        _err.print(
            "[yellow]agedum:[/] no AGENTS.md or .agents/skills/ (project or global) — "
            "running the command with no injected context."
        )
    dest = Path(tempfile.mkdtemp(prefix=f"agedum-{mode}-"))
    try:
        plan = _COMPILERS[mode](project, global_, dest)
        _inject_config_files(plan, project.root, dest, config_files)
        with _maybe_proxy(mode), _maybe_codex_proxy(mode, command) as run_command:
            return run_virtualfs(
                project.root, plan, run_command, close_stdin=close_stdin, sandbox=sandbox
            )
    except LauncherError as exc:
        _err.print(f"[red]agedum:[/] {exc}")
        return 1
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _inject_config_files(
    plan: Plan, project_root: Path, dest: Path, config_files: tuple[tuple[str, str, bool], ...]
) -> None:
    """Write each agedum-generated config file into ``dest`` and bind it at its target.

    ``config_files`` are ``(target, content, merge_json)`` triples: ``target`` is
    project-root-relative (reasonix's ``reasonix.toml``) or absolute (pi's user-scope
    ``~/.pi/agent/models.json`` + ``settings.json``); ``merge_json`` deep-merges ``content``
    onto any existing JSON at the target so an injected user-scope file augments rather than
    masks the user's own. The bind goes through the same launcher path as every other bind, so
    ``assert_safe`` still refuses a git-tracked target — a provider config dropped over a
    tracked file would otherwise be committable.
    """
    for target_spec, content, merge_json in config_files:
        spec = Path(target_spec)
        if spec.is_absolute():
            target = spec
            staged = Path(*spec.parts[1:])  # strip the leading "/" for a writable stage path
        else:
            target = project_root / target_spec
            staged = Path(target_spec)
        out = dest / "config-files" / staged
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(merge_json_onto_file(target, content) if merge_json else content)
        plan.binds.append((out, target))
        plan.origins[target] = f"<agedum-generated {target_spec}>"


@contextmanager
def _maybe_proxy(mode: str) -> Iterator[None]:
    """Interpose a local proxy in front of ``ANTHROPIC_BASE_URL`` when the provider opted in.

    Two mutually-exclusive proxies, both claude-only and both repointing the child's
    ``ANTHROPIC_BASE_URL`` at a localhost address for the duration of the wrapped command:

    * ``AGEDUM_FOLD_SYSTEM_MESSAGES=1`` (config ``foldSystemMessages``) → a ``FoldProxy``
      that folds ``system``-role messages into the top-level ``system`` field.
    * ``AGEDUM_TRANSLATE_OPENAI=1`` (config ``upstreamApi: openai-completions``) → a
      ``TranslateProxy`` that translates Anthropic⇄OpenAI so Claude Code can drive an
      OpenAI ``/v1/chat/completions`` upstream.

    A no-op for other harnesses and when neither flag is set.
    """
    upstream = os.environ.get("ANTHROPIC_BASE_URL", "")
    translate = os.environ.get("AGEDUM_TRANSLATE_OPENAI") == "1"
    fold = os.environ.get("AGEDUM_FOLD_SYSTEM_MESSAGES") == "1"
    if mode != "claude" or not upstream or not (translate or fold):
        yield
        return

    if translate:
        from agedum.proxy import TranslateProxy

        proxy = TranslateProxy(
            upstream,
            model=os.environ.get("ANTHROPIC_MODEL", ""),
            reasoning_effort=os.environ.get("CLAUDE_CODE_EFFORT_LEVEL", ""),
        )
    else:
        from agedum.proxy import FoldProxy

        proxy = FoldProxy(upstream)

    with proxy:
        os.environ["ANTHROPIC_BASE_URL"] = proxy.base_url
        try:
            yield
        finally:
            os.environ["ANTHROPIC_BASE_URL"] = upstream


@contextmanager
def _maybe_codex_proxy(mode: str, command: list[str]) -> Iterator[list[str]]:
    """Interpose the Responses↔Chat translation proxy for a codex launch against a
    Chat-Completions endpoint; yield the command to actually run.

    Enabled by ``AGEDUM_CODEX_CHAT_UPSTREAM`` (set by a codex provider config's
    ``chatCompletions: true``). codex speaks only the Responses API, but the upstream serves
    Chat Completions, so a local :class:`~agedum.proxy.ResponsesToChatProxy` translates both
    directions. The proxy's ephemeral address replaces the upstream in codex's
    ``-c model_providers.<id>.base_url`` override; codex — inside the bwrap namespace, which
    shares the host loopback — reaches it at ``127.0.0.1``. A no-op for any other harness or
    when the var is unset (the original command is yielded unchanged).
    """
    upstream = os.environ.get("AGEDUM_CODEX_CHAT_UPSTREAM", "")
    if mode != "codex" or not upstream:
        yield command
        return

    from agedum.proxy import ResponsesToChatProxy

    with ResponsesToChatProxy(upstream) as proxy:
        yield _rewrite_codex_base_url(command, proxy.base_url)


def _rewrite_codex_base_url(command: list[str], base_url: str) -> list[str]:
    """Return ``command`` with the codex ``-c model_providers.<id>.base_url="..."`` value
    swapped for ``base_url`` (the live proxy address)."""
    prefix = f"model_providers.{CODEX_PROVIDER_NAME}.base_url="
    return [
        f'{prefix}"{base_url}"' if isinstance(arg, str) and arg.startswith(prefix) else arg
        for arg in command
    ]
