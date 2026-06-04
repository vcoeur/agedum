"""Resolve a provider config JSON into the launch environment and command.

``agedum <name|path>`` reads a condash-style provider config and computes, at run
time: the variables to export/unset and the base command for the harness. The
per-harness mapping mirrors condash's pre-4.0 agent launcher (``buildClaudeSpawn`` /
``buildKimiSpawn`` / ``buildOpencodeSpawn``).

Unlike the retired ``--build-script`` codegen — which emitted a shell wrapper that
sourced the ``.env`` itself, so agedum never saw a token — this path reads the env
file (``${AGENTS_ENV_FILE:-~/.config/agents/.env}``) into the agedum process and sets
the resolved values in the child environment.

Resolution: ``agedum <value>`` where ``value`` is a **path** (it contains ``/`` or
ends in ``.json``; absolute as-is, else relative to CWD) or a **provider name**
(resolved to ``${AGENTS_PROVIDERS_DIR:-~/.config/agents/providers}/<name>.json``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HARNESSES = ("claude", "kimi", "opencode", "cline", "reasonix", "aider")

# opencode's built-in agent names keep opencode's own mode; ``primary`` only
# applies to custom agents.
OPENCODE_BUILTINS = frozenset({"build", "plan", "general", "explore", "scout"})

# A per-harness env/command builder's result:
#   (env_to_set, env_to_unset, base_command, config_files)
# config_files is a tuple of (project-root-relative target, content) pairs the launcher
# writes into the namespace — empty for every harness except a custom-endpoint reasonix.
BuilderResult = tuple[dict[str, str], list[str], list[str], tuple[tuple[str, str], ...]]


class ProviderError(RuntimeError):
    """A provider config could not be resolved into a launch."""


@dataclass(frozen=True)
class Launch:
    """A resolved provider launch: env to set/unset plus the base command.

    ``secrets`` names the env vars whose values must be masked in ``--dry-run``.
    ``config_files`` are agedum-generated config files a harness needs on disk
    (``(project-root-relative target, content)`` pairs); the launcher writes each into
    the namespace at the project root. reasonix uses it to inject a ``reasonix.toml``
    that defines a custom-endpoint provider — the others leave it empty.
    """

    harness: str
    label: str
    env: dict[str, str] = field(default_factory=dict)
    unset: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    secrets: frozenset[str] = frozenset()
    config_files: tuple[tuple[str, str], ...] = ()


def default_env_file() -> Path:
    """The env file to read secrets from: ``$AGENTS_ENV_FILE`` or
    ``~/.config/agents/.env``."""
    override = os.environ.get("AGENTS_ENV_FILE")
    return Path(override).expanduser() if override else Path.home() / ".config" / "agents" / ".env"


def providers_dir() -> Path:
    """The dir provider names resolve against: ``$AGENTS_PROVIDERS_DIR`` or
    ``~/.config/agents/providers``."""
    override = os.environ.get("AGENTS_PROVIDERS_DIR")
    return (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "agents" / "providers"
    )


def resolve_config_path(value: str, base_dir: Path | None = None) -> Path:
    """Resolve a CLI ``value`` to a config path.

    A ``value`` containing ``/`` or ending in ``.json`` is a path (absolute as-is,
    else relative to CWD); anything else is a provider name resolved to
    ``<providers_dir>/<name>.json``.
    """
    if "/" in value or value.endswith(".json"):
        return Path(value).expanduser()
    return (base_dir or providers_dir()) / f"{value}.json"


def load_config(path: Path) -> dict:
    """Read and parse a provider config JSON; raise :class:`ProviderError` on failure."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ProviderError(f"cannot read provider config {path}: {exc}") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ProviderError(
            f"provider config {path} must be a JSON object, not {type(config).__name__}"
        )
    return config


@dataclass(frozen=True)
class ProviderSummary:
    """One row of ``agedum --providers``: a provider config reduced to its listing fields.

    ``name`` is the bare name passed to ``agedum <name>`` (the file stem). ``harness`` and
    ``model`` are parsed from the config (``None`` when absent). ``error`` is set instead
    when the file could not be read or parsed, so a single bad config never aborts the
    listing.
    """

    name: str
    path: Path
    harness: str | None = None
    model: str | None = None
    error: str | None = None


def list_providers(directory: Path | None = None) -> list[ProviderSummary]:
    """Summarise every ``*.json`` provider config in ``directory`` (default:
    :func:`providers_dir`), sorted by name.

    Each entry carries the launch ``name`` plus the parsed ``harness`` / ``model``; an
    unreadable or invalid config yields a summary with ``error`` set and the parsed fields
    ``None`` rather than raising. A missing directory yields an empty list.
    """
    target = directory or providers_dir()
    summaries: list[ProviderSummary] = []
    for path in sorted(target.glob("*.json")):
        name = path.stem
        try:
            config = load_config(path)
        except ProviderError as exc:
            summaries.append(ProviderSummary(name, path, error=str(exc)))
            continue
        harness = config.get("harness")
        block = config.get("config")
        model = str(block.get("model") or "").strip() if isinstance(block, dict) else ""
        summaries.append(
            ProviderSummary(
                name,
                path,
                harness=harness if isinstance(harness, str) else None,
                model=model or None,
            )
        )
    return summaries


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` ``.env`` (no variable expansion).

    Honours an optional ``export `` prefix and surrounding single/double quotes; skips
    blank lines and ``#`` comments. Mirrors the subset the old generated wrapper relied
    on when it ran ``source "$env_file"``.
    """
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("export ").lstrip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def provider_label(config: dict) -> str:
    """The human label for a provider: ``slug`` else ``name`` else ``harness``."""
    return str(config.get("slug") or config.get("name") or config.get("harness") or "provider")


def required_env(config: dict) -> list[str]:
    """The env vars the launch validates + exports: the declared ``requiredEnv`` list
    with ``secretEnv`` appended if absent. Order is stable."""
    secret = str(config.get("secretEnv") or "").strip()
    result: list[str] = []
    declared = config.get("requiredEnv")
    if isinstance(declared, list):
        for value in declared:
            name = str(value).strip()
            if name and name not in result:
                result.append(name)
    if secret and secret not in result:
        result.append(secret)
    # An opencode providerDef's key env var is validated + exported even if the author
    # forgot to list it, so the apiKey baked into the config doc always has a value.
    block = config.get("config")
    if isinstance(block, dict):
        for provider_def in _provider_defs(block.get("providerDef")):
            api_key_env = str(provider_def.get("apiKeyEnv") or "").strip()
            if api_key_env and api_key_env not in result:
                result.append(api_key_env)
    return result


def build_launch(config: dict, base_env: dict[str, str]) -> Launch:
    """Resolve a parsed provider ``config`` into a :class:`Launch` using ``base_env``
    (typically ``os.environ`` overlaid with the parsed ``.env``).

    Validates the harness and that every required var is present and non-empty in
    ``base_env``; raises :class:`ProviderError` otherwise.
    """
    harness = config.get("harness")
    if harness not in HARNESSES:
        raise ProviderError(
            f"unsupported or missing harness {harness!r}; expected one of {', '.join(HARNESSES)}"
        )
    label = provider_label(config)
    block = config.get("config") or {}
    if not isinstance(block, dict):
        raise ProviderError("`config` must be a JSON object")
    secret_env = str(config.get("secretEnv") or "").strip()
    required = required_env(config)

    for name in required:
        if not base_env.get(name):
            raise ProviderError(f"{name} is required by provider {label} but is not set")

    # Required vars (incl. the secret) are exported into the child verbatim — kimi reads
    # its token this way, and it harmlessly mirrors the old `export VAR=...` lines.
    env: dict[str, str] = {name: base_env[name] for name in required}

    builders = {
        "claude": _claude_env,
        "kimi": _kimi_env,
        "opencode": _opencode_env,
        "cline": _cline_env,
        "reasonix": _reasonix_env,
        "aider": _aider_env,
    }
    extra, unset, command, config_files = builders[harness](block, secret_env, base_env)
    env.update(extra)

    secrets = set(required)
    secrets.update(var for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY") if var in env)
    # An opencode providerDef bakes the API key value into OPENCODE_CONFIG_CONTENT, so
    # mask the whole document in --dry-run.
    if _provider_defs(block.get("providerDef")) and "OPENCODE_CONFIG_CONTENT" in env:
        secrets.add("OPENCODE_CONFIG_CONTENT")
    return Launch(
        harness=harness,
        label=label,
        env=env,
        unset=unset,
        command=command,
        secrets=frozenset(secrets),
        config_files=tuple(config_files),
    )


def with_prompt(launch: Launch, rest: list[str], text: str, *, interactive: bool) -> list[str]:
    """Build the harness argv that seeds an initial prompt (agedum ``--prompt``/``--run``).

    ``interactive`` (agedum ``--prompt``) seeds the prompt but keeps the session open;
    otherwise (agedum ``--run``) the harness runs the prompt once, non-interactively, and
    exits. Each harness seeds a prompt differently, so the mapping is explicit:

    * **claude** — a positional prompt seeds an interactive session; ``--print`` runs and
      exits (``claude "<text>"`` vs ``claude --print "<text>"``).
    * **kimi** — ``--prompt`` seeds the agent (interactive by default); adding ``--print``
      makes that invocation non-interactive (``kimi --prompt "<text>" [--print]``).
    * **opencode** — top-level ``--prompt`` seeds the TUI; the ``run`` subcommand runs and
      exits (``opencode --prompt "<text>"`` vs ``opencode run "<text>"``).
    * **cline** — a positional prompt is the seed either way; ``--tui`` is what opens the
      interactive TUI (seeded via Cline's ``initialPrompt``), while a bare positional runs
      once in act mode and exits (``cline --tui "<text>"`` vs ``cline "<text>"``).
    * **reasonix** — only the ``run`` subcommand seeds a prompt (it takes the task as a
      positional and exits); ``chat`` has no way to pre-seed an interactive session. So
      ``--run`` swaps the base ``chat`` subcommand for ``run`` (``reasonix run "<text>"``),
      and ``--prompt`` (which must stay interactive) raises :class:`ProviderError` rather
      than guess — condash then falls back to spawn-and-type for that harness.
    * **aider** — ``--message "<text>"`` runs one message and exits (it disables chat mode),
      which is ``--run``. aider has no "seed then stay interactive" mode, so ``--prompt``
      raises :class:`ProviderError` like reasonix.

    A harness with no known prompt-seeding convention raises :class:`ProviderError` —
    agedum fails loudly rather than silently launching the wrong way. ``rest`` (harness
    passthrough args) is preserved before the prompt text.
    """
    binary, *base_flags = launch.command
    harness = launch.harness
    if harness == "claude":
        mode_flags = [] if interactive else ["--print"]
        return [binary, *base_flags, *rest, *mode_flags, text]
    if harness == "kimi":
        mode_flags = [] if interactive else ["--print"]
        return [binary, *base_flags, *rest, "--prompt", text, *mode_flags]
    if harness == "opencode":
        if interactive:
            return [binary, *base_flags, *rest, "--prompt", text]
        # The `run` subcommand must lead, before any passthrough args or the message.
        return [binary, "run", *base_flags, *rest, text]
    if harness == "cline":
        # The prompt is Cline's positional argument; --tui flips it to the interactive TUI
        # (seeded), its absence runs the task once and exits. Text stays last so commander
        # reads it as the positional.
        mode_flags = ["--tui"] if interactive else []
        return [binary, *base_flags, *rest, *mode_flags, text]
    if harness == "reasonix":
        # reasonix can't pre-seed an interactive `chat` — only `run` takes a task and exits.
        # So --prompt (interactive) has no target and fails loudly. base_flags starts with the
        # `chat` subcommand from _reasonix_env; --run swaps it for `run`, keeping --model.
        if interactive:
            raise ProviderError(
                "reasonix has no interactive prompt-seeding (`chat` cannot be pre-seeded); "
                "use --run for a one-shot task, or launch without --prompt for an "
                "interactive session"
            )
        sub_flags = base_flags[1:] if base_flags and base_flags[0] == "chat" else base_flags
        return [binary, "run", *sub_flags, *rest, text]
    if harness == "aider":
        # aider's `--message`/-m runs a single message then exits (disables chat mode) — that
        # is --run. There is no "seed then stay interactive" mode, so --prompt fails loudly
        # (condash then falls back to spawn-and-type), mirroring reasonix.
        if interactive:
            raise ProviderError(
                "aider has no interactive prompt-seeding (`--message` runs once and exits); "
                "use --run for a one-shot task, or launch without --prompt for an "
                "interactive session"
            )
        return [binary, *base_flags, *rest, "--message", text]
    raise ProviderError(
        f"harness {harness!r} has no known prompt-seeding flags; "
        "agedum --prompt/--run is not supported for it"
    )


# ---------------------------------------------------------------------------
# per-harness env/command builders -> (env_to_set, env_to_unset, base_command, config_files)
# ---------------------------------------------------------------------------


def _claude_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    base_url = str(block.get("baseUrl") or "").strip()
    if not base_url:
        # Native Claude: no provider overrides (the all-empty config). Run bare.
        return {}, [], ["claude"], ()
    if not secret_env:
        raise ProviderError("claude config has a baseUrl but no secretEnv to supply the API token")

    env: dict[str, str] = {"ANTHROPIC_BASE_URL": base_url}
    unset: list[str] = []
    token = base_env.get(secret_env, "")
    if str(block.get("authStyle") or "bearer").strip() == "apikey":
        env["ANTHROPIC_API_KEY"] = token
        unset.append("ANTHROPIC_AUTH_TOKEN")
    else:
        env["ANTHROPIC_AUTH_TOKEN"] = token
        unset.append("ANTHROPIC_API_KEY")

    for key, var in (
        ("model", "ANTHROPIC_MODEL"),
        ("smallFastModel", "ANTHROPIC_SMALL_FAST_MODEL"),
        ("haikuAlias", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        ("sonnetAlias", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
        ("opusAlias", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        ("subagentModel", "CLAUDE_CODE_SUBAGENT_MODEL"),
    ):
        value = str(block.get(key) or "").strip()
        if value:
            env[var] = value

    max_tokens = block.get("maxContextTokens") or 0
    if isinstance(max_tokens, (int, float)) and int(max_tokens) > 0:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(int(max_tokens))

    effort = str(block.get("effortLevel") or "").strip()
    if effort:
        env["CLAUDE_CODE_EFFORT_LEVEL"] = effort

    for key, var in (
        ("disableCaching", "DISABLE_PROMPT_CACHING"),
        ("disable1M", "CLAUDE_CODE_DISABLE_1M_CONTEXT"),
        ("disableAdaptiveThinking", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"),
        ("disableTelemetry", "DISABLE_TELEMETRY"),
        ("disableErrorReporting", "DISABLE_ERROR_REPORTING"),
        ("disableClaudeApiSkill", "CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL"),
    ):
        if block.get(key) is True:
            env[var] = "1"

    # Strict Anthropic-compat endpoints (e.g. DeepSeek's /anthropic) reject a `system`
    # role inside `messages[]`. This flag makes agedum's wrapper interpose a local proxy
    # that folds those entries into the top-level `system` field — see agedum.proxy.
    if block.get("foldSystemMessages") is True:
        env["AGEDUM_FOLD_SYSTEM_MESSAGES"] = "1"

    # Defensive: never let a stray cloud-provider switch leak into the child.
    unset += [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
    ]
    return env, unset, ["claude"], ()


def _kimi_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # kimi's provider/model knobs are appended CLI flags, not env vars. The token
    # (secret_env) reaches the child via the required-env export in build_launch.
    command = ["kimi"]
    model = str(block.get("model") or "").strip()
    if model:
        command += ["--model", model]
    thinking = block.get("thinking")
    if thinking is True:
        command.append("--thinking")
    elif thinking is False:
        command.append("--no-thinking")
    if block.get("plan") is True:
        command.append("--plan")
    config_inline = str(block.get("configInline") or "").strip()
    if config_inline:
        command += ["--config", config_inline]
    return {}, [], command, ()


def _opencode_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    env: dict[str, str] = {}
    if block.get("disableExternalSkills") is True:
        env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
    document = _opencode_config_doc(block)
    for provider_def in _provider_defs(block.get("providerDef")):
        document = _apply_provider_def(document, provider_def, base_env)
    if document:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(document, sort_keys=True)
    return env, [], ["opencode"], ()


def _cline_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # Cline's provider/model knobs are appended CLI flags (like kimi). Unlike the other
    # harnesses, Cline takes the token as a per-run flag (`--key`), so the secret lands in
    # argv (visible in the process list while Cline runs) — its documented mechanism, not
    # agedum's choice. build_launch still exports secret_env into the child env via the
    # required-env path, so the value is in Launch.secrets and --dry-run masks it (the
    # command print redacts secret values; Cline is the first harness to put one there).
    if str(block.get("baseUrl") or "").strip():
        raise ProviderError(
            "cline has no base-URL flag; configure the endpoint in the named Cline "
            "provider (`cline auth`) and select it with `provider`, not `baseUrl`"
        )
    command = ["cline"]
    model = str(block.get("model") or "").strip()
    if model:
        command += ["--model", model]
    provider = str(block.get("provider") or "").strip()
    if provider:
        command += ["--provider", provider]
    effort = str(block.get("effortLevel") or "").strip()
    if effort:
        command += ["--thinking", effort]
    if block.get("plan") is True:
        command.append("--plan")
    if secret_env:
        token = base_env.get(secret_env, "")
        if token:
            command += ["--key", token]
    return {}, [], command, ()


def _aider_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # aider drives models through litellm: the API token reaches it through the required-env
    # export under its conventional name (OPENAI_API_KEY / ANTHROPIC_API_KEY / DEEPSEEK_API_KEY
    # / …, per the chosen model's provider), so no key flag is appended and no secret lands in
    # argv. model / git / endpoint are CLI flags, which override any on-disk .aider.conf.yml.
    env: dict[str, str] = {}
    command = ["aider"]
    for key, flag in (
        ("model", "--model"),
        ("weakModel", "--weak-model"),
        ("editorModel", "--editor-model"),
        ("reasoningEffort", "--reasoning-effort"),
    ):
        value = str(block.get(key) or "").strip()
        if value:
            command += [flag, value]

    # A custom OpenAI-compatible endpoint: litellm reads its base URL from OPENAI_API_BASE
    # (pair it with an `openai/<name>` model and OPENAI_API_KEY in secretEnv).
    base_url = str(block.get("baseUrl") or "").strip()
    if base_url:
        env["OPENAI_API_BASE"] = base_url

    # Git integration defaults OFF: agedum's launch namespace shares the real .git, so aider's
    # default --auto-commits would write to the real repo. `git: true` opts back in (hazardous
    # in the shared namespace); with git on, `autoCommits: false` still suppresses commits.
    if block.get("git") is True:
        if block.get("autoCommits") is False:
            command.append("--no-auto-commits")
    else:
        command.append("--no-git")

    if block.get("yesAlways") is True:
        command.append("--yes-always")
    return env, [], command, ()


# The provider name agedum assigns to a generated custom-endpoint reasonix provider.
# Fixed + always a valid identifier; the user never types it (agedum selects it via
# --model), and each launch gets its own reasonix.toml so there is no cross-launch clash.
REASONIX_PROVIDER_NAME = "agedum"


def _reasonix_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # reasonix is DeepSeek-native: its provider/model selection is a CLI flag on the
    # `chat`/`run` subcommand, and the API token reaches the child via the required-env
    # export — reasonix reads it through the selected provider's `api_key_env` (e.g.
    # DEEPSEEK_API_KEY), like kimi.
    base_url = str(block.get("baseUrl") or "").strip()
    model = str(block.get("model") or "").strip()
    provider_defs = _provider_defs(block.get("providerDef"))
    agent_lines = _reasonix_agent_lines(block)

    # A reasonix.toml is generated whenever reasonix needs on-disk-only config it has no flag
    # for: a custom endpoint (`baseUrl` / `providerDef`) or the `[agent]` two-model routing
    # (`subagentModel` / `plannerModel` / `autoPlan`). Otherwise `model` just selects a
    # provider reasonix already knows by name (a built-in like `deepseek-pro`, or one in the
    # user's reasonix.toml) via --model, and nothing is injected. `chat` is the interactive
    # subcommand (a bare `reasonix` only shows a welcome screen); with_prompt swaps it for
    # `run` on --run. Both `chat` and `run` accept `--model`.
    if not (base_url or provider_defs or agent_lines):
        command = ["reasonix", "chat"]
        if model:
            command += ["--model", model]
        return {}, [], command, ()

    if base_url and provider_defs:
        raise ProviderError(
            "reasonix config sets both `baseUrl` and `providerDef`; use one — `baseUrl` for a "
            "single inline endpoint, `providerDef` for one or more named providers"
        )
    if not model:
        raise ProviderError(
            "reasonix needs `model` (the executor) to set as default_model — a built-in / "
            "providerDef provider name, or (with baseUrl) the upstream model id at that endpoint"
        )

    # The generated ./reasonix.toml is bound at the project root (reasonix's highest-priority
    # TOML source). `[[providers]]` replaces the providers list wholesale while the user
    # config's scalars + plugins survive the merge — so a config with NO providers block (only
    # default_model + [agent], referencing built-ins) keeps reasonix's built-in providers.
    # `baseUrl` is the single-inline shorthand (one provider named `agedum`, `model` = its
    # upstream id); `providerDef` is the explicit multi-provider form whose entries `model` /
    # `subagentModel` / `plannerModel` reference by id.
    if base_url:
        default_model = REASONIX_PROVIDER_NAME
        provider_blocks = [
            _reasonix_provider_block(
                name=REASONIX_PROVIDER_NAME,
                kind=str(block.get("kind") or "openai").strip() or "openai",
                base_url=base_url,
                model=model,
                api_key_env=secret_env,
            )
        ]
    else:
        default_model = model
        provider_blocks = [_reasonix_provider_block_from_def(pd) for pd in provider_defs]

    toml = _reasonix_toml(default_model, agent_lines, provider_blocks)
    command = ["reasonix", "chat", "--model", default_model]
    return {}, [], command, (("reasonix.toml", toml),)


def _reasonix_agent_lines(block: dict) -> list[str]:
    """The ``[agent]`` body lines for reasonix two-model routing, or ``[]`` when none are set.

    ``subagentModel`` → ``subagent_model`` (default model for runAs=subagent skills),
    ``plannerModel`` → ``planner_model`` (planner/executor two-model collaboration),
    ``autoPlan`` → ``auto_plan`` (``off`` | ``ask`` | ``on``). Each references a provider name
    reasonix knows (a built-in or a ``providerDef`` id).
    """
    lines: list[str] = []
    subagent = str(block.get("subagentModel") or "").strip()
    if subagent:
        lines.append(f'subagent_model = "{_toml_escape(subagent)}"')
    planner = str(block.get("plannerModel") or "").strip()
    if planner:
        lines.append(f'planner_model = "{_toml_escape(planner)}"')
    auto_plan = str(block.get("autoPlan") or "").strip()
    if auto_plan:
        if auto_plan not in ("off", "ask", "on"):
            raise ProviderError("reasonix `autoPlan` must be one of: off, ask, on")
        lines.append(f'auto_plan = "{_toml_escape(auto_plan)}"')
    return lines


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML double-quoted basic string (backslash, then quote)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _reasonix_provider_block_from_def(provider_def: dict) -> str:
    """Render one ``[[providers]]`` block from a reasonix ``providerDef`` entry.

    Fields: ``id`` → name, ``baseUrl`` → base_url, ``model`` → model, ``apiKeyEnv`` →
    api_key_env (optional; omitted for a keyless endpoint), ``kind`` (default ``openai``).
    ``id`` / ``baseUrl`` / ``model`` are required.
    """
    fields = {key: str(provider_def.get(key) or "").strip() for key in ("id", "baseUrl", "model")}
    missing = [key for key, value in fields.items() if not value]
    if missing:
        raise ProviderError(
            f"reasonix providerDef is missing required field(s): {', '.join(missing)}"
        )
    return _reasonix_provider_block(
        name=fields["id"],
        kind=str(provider_def.get("kind") or "openai").strip() or "openai",
        base_url=fields["baseUrl"],
        model=fields["model"],
        api_key_env=str(provider_def.get("apiKeyEnv") or "").strip(),
    )


def _reasonix_provider_block(
    *, name: str, kind: str, base_url: str, model: str, api_key_env: str
) -> str:
    """Render one reasonix ``[[providers]]`` block. The key is referenced by env-var name
    (``api_key_env``), never its value; ``api_key_env`` is omitted when empty (keyless)."""
    lines = [
        "[[providers]]",
        f'name = "{_toml_escape(name)}"',
        f'kind = "{_toml_escape(kind)}"',
        f'base_url = "{_toml_escape(base_url)}"',
        f'model = "{_toml_escape(model)}"',
    ]
    if api_key_env:
        lines.append(f'api_key_env = "{_toml_escape(api_key_env)}"')
    return "\n".join(lines)


def _reasonix_toml(default_model: str, agent_lines: list[str], provider_blocks: list[str]) -> str:
    """Assemble a reasonix.toml: ``default_model``, an optional ``[agent]`` section, then the
    ``[[providers]]`` blocks. Carries no secret (keys are referenced by env-var name)."""
    parts = [f'default_model = "{_toml_escape(default_model)}"']
    if agent_lines:
        parts.append("[agent]\n" + "\n".join(agent_lines))
    parts.extend(provider_blocks)
    return "\n\n".join(parts) + "\n"


def _provider_defs(value: object) -> list[dict]:
    """Normalise a ``providerDef`` config value into a list of provider-def dicts.

    Accepts a single dict (one provider) or a list of dicts (several providers — e.g. a
    primary model and a fast-subagent model that live on different providers, each
    needing its own baked-in key). ``None`` yields an empty list. Order is preserved so
    later defs deep-merge over earlier ones.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                raise ProviderError("each `providerDef` entry must be a JSON object")
        return value
    raise ProviderError("`providerDef` must be a JSON object or a list of them")


def _apply_provider_def(document: dict, provider_def: object, base_env: dict[str, str]) -> dict:
    """Deep-merge one explicit provider definition into the opencode ``document``.

    A providerDef ({id, npm, baseUrl, apiKeyEnv}) becomes
    ``provider.<id> = {npm, options: {baseURL, apiKey}}``, with ``apiKey`` set to the
    *value* of ``apiKeyEnv`` from ``base_env``. Unlike opencode's ``{env:…}``
    substitution — unreliable for a custom provider's ``options.apiKey`` — the resolved
    key is written straight into the config doc agedum hands the child (the same
    in-process token handling ``_claude_env`` already uses for ``ANTHROPIC_AUTH_TOKEN``).
    The config may carry a single providerDef or a list of them (see ``_provider_defs``);
    this applies one.
    """
    if not isinstance(provider_def, dict):
        raise ProviderError("`providerDef` must be a JSON object")
    fields = {
        "id": str(provider_def.get("id") or "").strip(),
        "npm": str(provider_def.get("npm") or "").strip(),
        "baseUrl": str(provider_def.get("baseUrl") or "").strip(),
        "apiKeyEnv": str(provider_def.get("apiKeyEnv") or "").strip(),
    }
    missing = [key for key, value in fields.items() if not value]
    if missing:
        raise ProviderError(f"providerDef is missing required field(s): {', '.join(missing)}")
    entry: dict = {
        "npm": fields["npm"],
        "options": {"baseURL": fields["baseUrl"], "apiKey": base_env.get(fields["apiKeyEnv"], "")},
    }
    name = str(provider_def.get("name") or "").strip()
    if name:
        entry["name"] = name
    providers = dict(document.get("provider") or {})
    providers[fields["id"]] = _deep_merge(providers.get(fields["id"], {}), entry)
    merged = dict(document)
    merged["provider"] = providers
    return merged


def _opencode_config_doc(block: dict) -> dict:
    """Build the ``OPENCODE_CONFIG_CONTENT`` JSON document from an opencode config."""
    document: dict = {}
    model = str(block.get("model") or "").strip()
    if model:
        document["model"] = model

    # Flat `effortLevel` is a convenience alias for the default model's
    # reasoningEffort; an explicit defaultOptions.reasoningEffort wins.
    default_options = dict(block.get("defaultOptions") or {})
    flat_effort = str(block.get("effortLevel") or "").strip()
    if flat_effort and not str(default_options.get("reasoningEffort") or "").strip():
        default_options["reasoningEffort"] = flat_effort

    options = _clean_options(default_options)
    if options and model and "/" in model:
        provider_id, model_id = model.split("/", 1)
        document["provider"] = {provider_id: {"models": {model_id: {"options": options}}}}

    agents: dict = {}
    rows = block.get("agentOptions")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("agent") or "").strip()
            if not name:
                continue
            entry: dict = {}
            row_model = str(row.get("model") or "").strip()
            if row_model:
                entry["model"] = row_model
            row_options = _clean_options(row)
            if row_options:
                entry["options"] = row_options
            if row.get("primary") is True and name not in OPENCODE_BUILTINS:
                entry["mode"] = "primary"
            if entry:
                agents[name] = entry
    if agents:
        document["agent"] = agents

    # `opencodeConfig` is a literal opencode config object, deep-merged into the
    # document last so it wins on conflict with the modeled keys — the escape hatch
    # for any opencode option agedum does not model, written in opencode's own format.
    passthrough = block.get("opencodeConfig")
    if passthrough is not None:
        if not isinstance(passthrough, dict):
            raise ProviderError("opencodeConfig must be a JSON object")
        document = _deep_merge(document, passthrough)

    # Auto-inject the bundled transcript-capture plugin so any terminal capturer
    # (condash, `script`, tmux, …) can recover a clean transcript from opencode's
    # alternate-screen TUI. The plugin emits a neutral OSC the terminal ignores;
    # naming no viewer, agedum stays viewer-agnostic. Added last + appended so it
    # unions with any `opencodeConfig.plugin`. Opt out with `"emitTranscript": false`.
    if block.get("emitTranscript") is not False:
        plugins = list(document.get("plugin") or [])
        plugin_path = _transcript_plugin_path()
        if plugin_path not in plugins:
            plugins.append(plugin_path)
        document["plugin"] = plugins

    return document


def _transcript_plugin_path() -> str:
    """Absolute path of the bundled opencode transcript-capture plugin.

    Shipped inside the agedum package (``agedum/assets/opencode/transcript-osc.js``)
    and resolved on disk; agedum's bwrap launch binds the whole real filesystem, so
    the path is visible to opencode inside the namespace.
    """
    return str(Path(__file__).resolve().parent / "assets" / "opencode" / "transcript-osc.js")


def _clean_options(source: dict) -> dict:
    """Pull the three non-empty opencode model options out of a dict, in fixed order."""
    options: dict = {}
    for key in ("reasoningEffort", "textVerbosity", "reasoningSummary"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            options[key] = value.strip()
    return options


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into `base` (overlay wins); returns a new dict."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
