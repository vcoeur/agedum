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
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agedum.harness import Sandbox, codex_config_dir, kimi_config_dir, pi_agent_dir

HARNESSES = ("claude", "kimi", "opencode", "cline", "reasonix", "aider", "pi", "codex")

# opencode's built-in agent names keep opencode's own mode; ``primary`` only
# applies to custom agents.
OPENCODE_BUILTINS = frozenset({"build", "plan", "general", "explore", "scout"})

# pi-subagents' eight built-in agents — `subagentModel` routes all of them through
# pi's settings.json `subagents.agentOverrides`.
PI_SUBAGENT_BUILTINS = (
    "scout",
    "researcher",
    "planner",
    "worker",
    "reviewer",
    "context-builder",
    "oracle",
    "delegate",
)

# A per-harness env/command builder's result:
#   (env_to_set, env_to_unset, base_command, config_files)
# config_files is a tuple of (target, content, merge_json) triples the launcher writes into
# the namespace: `target` is project-root-relative (reasonix's reasonix.toml) or absolute
# (pi's user-scope ~/.pi/agent/models.json + settings.json); `merge_json` deep-merges the
# content onto any existing JSON file at the target. Empty for every harness without a
# generated on-disk config.
BuilderResult = tuple[dict[str, str], list[str], list[str], tuple[tuple[str, str, bool], ...]]


class ProviderError(RuntimeError):
    """A provider config could not be resolved into a launch."""


@dataclass(frozen=True)
class Launch:
    """A resolved provider launch: env to set/unset plus the base command.

    ``secrets`` names the env vars whose values must be masked in ``--dry-run``.
    ``config_files`` are agedum-generated config files a harness needs on disk
    (``(target, content, merge_json)`` triples); the launcher writes each into the
    namespace. ``target`` is project-root-relative (reasonix's ``reasonix.toml``) or
    absolute (pi's user-scope ``~/.pi/agent/models.json`` + ``settings.json``);
    ``merge_json`` deep-merges ``content`` onto any existing JSON file at the target so an
    injected user-scope config augments rather than masks the user's own. Empty for a
    harness without a generated on-disk config.

    ``warnings`` are non-fatal advisories surfaced at launch (and in ``--dry-run``) — e.g. a
    pi provider whose `requireExtensions` names a pi extension that is not installed on the
    host. They never block the launch (use a fail-loud ``ProviderError`` for that).

    ``sandbox`` (when set) requests filesystem confinement: the host is mounted read-only and
    only the working set is writable (see :class:`agedum.harness.Sandbox`). ``None`` keeps the
    legacy full read-write host bind.
    """

    harness: str
    label: str
    env: dict[str, str] = field(default_factory=dict)
    unset: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    secrets: frozenset[str] = frozenset()
    config_files: tuple[tuple[str, str, bool], ...] = ()
    warnings: tuple[str, ...] = ()
    sandbox: Sandbox | None = None


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
    """Resolve a config reference to a path, anchored at the providers root.

    A ``value`` starting with ``/`` is an absolute filesystem path; anything else resolves
    **relative to the providers root** (``base_dir`` or :func:`providers_dir`), so nested
    references like ``claude/deepseek`` or ``base/claude.json`` work. ``.json`` is appended
    when the value has no extension. The same rule resolves both the ``agedum <value>``
    argument and an ``extends`` reference; a path that does not exist surfaces as an error at
    load time (there is no fallback search).
    """
    candidate = Path(value) if value.startswith("/") else (base_dir or providers_dir()) / value
    if candidate.suffix != ".json":
        candidate = candidate.parent / f"{candidate.name}.json"
    return candidate


def load_config(path: Path) -> dict:
    """Read and parse a single provider config JSON file; raise :class:`ProviderError`.

    This is the raw, one-file load — it does **not** resolve ``extends``. Use
    :func:`load_merged_config` to get a config's effective (extends-resolved) form.
    """
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


# File-level meta keys: consumed during resolution, never passed to the launch.
_META_KEYS = ("extends", "abstract")


def _without_meta(config: dict) -> dict:
    """A copy of ``config`` without the meta keys (``extends`` / ``abstract``).

    ``abstract`` is a property of the file as authored, not of the merged result, so it is
    dropped here — a config extending an abstract base never inherits its abstractness."""
    return {key: value for key, value in config.items() if key not in _META_KEYS}


def _extends_refs(config: dict) -> list[str]:
    """Normalise a config's ``extends`` (string, list, or absent) to a list of refs."""
    raw = config.get("extends")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise ProviderError("`extends` must be a string or a list of strings")


def load_merged_config(
    path: Path, base_dir: Path | None = None, _seen: frozenset[Path] | None = None
) -> dict:
    """Load a provider config and resolve its ``extends`` chain into one effective config.

    Each ``extends`` reference resolves by the providers-root rule (see
    :func:`resolve_config_path`); bases are deep-merged left→right and the extending config's
    own keys applied last (child wins). A base may itself ``extends`` (recursive). The meta
    keys (``extends`` / ``abstract``) are stripped from the result. A circular ``extends``
    chain raises :class:`ProviderError`.
    """
    providers = base_dir or providers_dir()
    resolved = path.resolve()
    seen = _seen or frozenset()
    if resolved in seen:
        raise ProviderError(f"circular extends involving {path}")
    seen = seen | {resolved}
    raw = load_config(path)
    merged: dict = {}
    for ref in _extends_refs(raw):
        base = load_merged_config(resolve_config_path(ref, providers), providers, seen)
        merged = _deep_merge(merged, base)
    return _deep_merge(merged, _without_meta(raw))


@dataclass(frozen=True)
class ProviderSummary:
    """One row of ``agedum --providers``: a provider config reduced to its listing fields.

    ``name`` is the reference passed to ``agedum <name>`` — the config's path **relative to
    the providers root**, without ``.json`` (e.g. ``claude/deepseek``). ``harness`` and
    ``model`` come from the config's *effective* (extends-resolved) form (``None`` when
    absent). ``error`` is set instead when the file could not be read, parsed, or resolved,
    so a single bad config never aborts the listing.
    """

    name: str
    path: Path
    harness: str | None = None
    model: str | None = None
    error: str | None = None


def list_providers(directory: Path | None = None) -> list[ProviderSummary]:
    """Summarise every launchable provider config under ``directory`` (default:
    :func:`providers_dir`), recursively, sorted by name.

    Walks subdirectories; each config's ``name`` is its path relative to the root (no
    ``.json``). ``abstract: true`` configs (bases) are skipped. ``harness`` / ``model`` come
    from the effective (extends-resolved) config; an unreadable, invalid, or
    unresolvable config yields a summary with ``error`` set rather than raising. A missing
    directory yields an empty list.
    """
    target = directory or providers_dir()
    summaries: list[ProviderSummary] = []
    if not target.is_dir():
        return summaries
    for path in sorted(target.rglob("*.json")):
        name = path.relative_to(target).with_suffix("").as_posix()
        try:
            raw = load_config(path)
        except ProviderError as exc:
            summaries.append(ProviderSummary(name, path, error=str(exc)))
            continue
        if raw.get("abstract") is True:
            continue  # a base, not a launchable provider
        try:
            config = load_merged_config(path, target)
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
    blank lines and ``#`` comments, including a trailing `` # comment`` after an unquoted
    value (a quoted value keeps its ``#`` verbatim). Mirrors the subset the old generated
    wrapper relied on when it ran ``source "$env_file"``.
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
        else:
            # `KEY=val # comment` under `source` sets "val" — the comment is not part
            # of the value. Only a whitespace-preceded `#` counts; `val#ue` stays intact.
            value = _strip_trailing_comment(value)
        if key:
            result[key] = value
    return result


def _strip_trailing_comment(value: str) -> str:
    """Drop a `` # comment`` tail from an unquoted ``.env`` value (sh word-splitting
    semantics: only a ``#`` preceded by whitespace starts a comment)."""
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in (" ", "\t"):
            return value[:index].rstrip()
    return value


def provider_label(config: dict) -> str:
    """Fallback label for a provider: ``slug`` else ``harness``.

    The canonical label is the **config path**, passed to :func:`build_launch` as ``label``
    (the provider's identity is its path now — the ``name`` field is gone). This fallback is
    only used when no path-based label is supplied (e.g. a direct ``build_launch`` call)."""
    return str(config.get("slug") or config.get("harness") or "provider")


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


def build_launch(config: dict, base_env: dict[str, str], *, label: str | None = None) -> Launch:
    """Resolve a parsed provider ``config`` into a :class:`Launch` using ``base_env``
    (typically ``os.environ`` overlaid with the parsed ``.env``).

    ``config`` is the *effective* config (already extends-resolved). ``label`` is the
    provider's display name — the config path the user invoked; it falls back to
    :func:`provider_label` when not given. Validates the harness and that every required var
    is present and non-empty in ``base_env``; raises :class:`ProviderError` otherwise.
    """
    harness = config.get("harness")
    if harness not in HARNESSES:
        raise ProviderError(
            f"unsupported or missing harness {harness!r}; expected one of {', '.join(HARNESSES)}"
        )
    label = label or provider_label(config)
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
        "pi": _pi_env,
        "codex": _codex_env,
    }
    extra, unset, command, config_files = builders[harness](block, secret_env, base_env)
    env.update(extra)

    secrets = set(required)
    secrets.update(var for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY") if var in env)
    # An opencode providerDef bakes the API key value into OPENCODE_CONFIG_CONTENT, so
    # mask the whole document in --dry-run.
    if _provider_defs(block.get("providerDef")) and "OPENCODE_CONFIG_CONTENT" in env:
        secrets.add("OPENCODE_CONFIG_CONTENT")
    # pi's requireExtensions gate: warn (or fail-loud, when strict) about pi extensions the
    # config relies on but the host has not installed — see _pi_extension_warnings.
    warnings = _pi_extension_warnings(block) if harness == "pi" else []
    return Launch(
        harness=harness,
        label=label,
        env=env,
        unset=unset,
        command=command,
        secrets=frozenset(secrets),
        config_files=tuple(config_files),
        warnings=tuple(warnings),
        sandbox=_parse_sandbox(config),
    )


def _parse_sandbox(config: dict) -> Sandbox | None:
    """Parse the optional top-level ``sandbox`` block into a :class:`Sandbox`.

    Presence of ``sandbox`` enables write-confinement (the launcher mounts the host
    read-only). ``readWrite`` is a list of path templates the agent may modify (``~`` /
    ``$VAR`` / ``${PROJECT_ROOT}`` resolved at launch). Absent ``sandbox`` → ``None`` (the
    legacy full read-write launch). The project root, agedum's own injection dirs, and the
    harness's own state/config dir (e.g. ``~/.cline``, ``~/.claude``) are always writable, so
    they need not be listed.
    """
    raw = config.get("sandbox")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProviderError("`sandbox` must be a JSON object")
    read_write = raw.get("readWrite", [])
    if not isinstance(read_write, list) or not all(isinstance(item, str) for item in read_write):
        raise ProviderError("`sandbox.readWrite` must be a list of path strings")
    cleaned = tuple(item.strip() for item in read_write if item.strip())
    return Sandbox(enabled=True, read_write=cleaned)


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
    * **pi** — the prompt is pi's positional argument either way; ``--print`` (``-p``) flips
      it to non-interactive (process-and-exit). So a bare positional seeds the interactive
      TUI and ``--print`` runs it once and exits (``pi "<text>"`` vs ``pi --print "<text>"``),
      the claude shape exactly.

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
    if harness == "pi":
        # pi takes the prompt as a positional either way; --print/-p flips it to
        # non-interactive (process-and-exit). Text stays last so pi reads it as the positional.
        mode_flags = [] if interactive else ["--print"]
        return [binary, *base_flags, *rest, *mode_flags, text]
    if harness == "codex":
        # codex takes a positional prompt to seed an interactive session; the `exec`
        # subcommand runs it once non-interactively and exits. `exec` must lead, before the
        # -m/-c flags and the prompt (`codex "<text>"` vs `codex exec "<text>"`), the opencode
        # `run` shape. base_flags are codex's -m/-c provider flags from _codex_env.
        if interactive:
            return [binary, *base_flags, *rest, text]
        return [binary, "exec", *base_flags, *rest, text]
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
        # A proxy option without a baseUrl has nothing to sit in front of — fail loudly
        # rather than silently run vanilla Claude against the real Anthropic API.
        if block.get("foldSystemMessages") is True or str(block.get("upstreamApi") or "").strip():
            raise ProviderError(
                "claude config sets a proxy option (foldSystemMessages / upstreamApi) but no "
                "baseUrl for it to proxy"
            )
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

    # The context-window size Claude Code auto-compacts against — set it to a non-Anthropic
    # upstream's real window (e.g. Kimi Code's 262144) so compaction fires at the right point.
    auto_compact = block.get("autoCompactWindow") or 0
    if isinstance(auto_compact, (int, float)) and int(auto_compact) > 0:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(int(auto_compact))

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
    fold = block.get("foldSystemMessages") is True

    # An OpenAI-only upstream (a `/v1/chat/completions` surface with no working Anthropic
    # `/v1/messages`) needs a translating proxy, not a folding one. `upstreamApi:
    # "openai-completions"` turns it on; "anthropic-messages" (or unset) is today's no-op.
    upstream_api = str(block.get("upstreamApi") or "").strip()
    if upstream_api and upstream_api not in ("anthropic-messages", "openai-completions"):
        raise ProviderError(
            f"claude config has an unknown upstreamApi {upstream_api!r}; expected "
            "'anthropic-messages' or 'openai-completions'"
        )
    translate = upstream_api == "openai-completions"
    if translate and fold:
        # The translator already produces a clean top-level `system`; folding is moot, and
        # only one proxy can sit in front of ANTHROPIC_BASE_URL at a time.
        raise ProviderError(
            "claude config sets both `upstreamApi: openai-completions` and "
            "`foldSystemMessages`; use one — the translator already folds the system prompt"
        )

    # OpenAI-translate refinements — only meaningful when the translating proxy is on:
    #  - openaiPromptCacheKey: inject a per-launch `prompt_cache_key` prefix-cache routing hint.
    #  - openaiThinking "toggle": map Anthropic `thinking` to an on/off `thinking:{type}` param
    #    (for models that support it, e.g. Kimi K2.6; NOT always-think models like k2.7-code).
    cache_hint = block.get("openaiPromptCacheKey") is True
    thinking_mode = str(block.get("openaiThinking") or "").strip()
    if (cache_hint or thinking_mode) and not translate:
        raise ProviderError(
            "claude config sets an OpenAI-translate option (openaiPromptCacheKey / "
            "openaiThinking) but not `upstreamApi: openai-completions`"
        )
    if thinking_mode and thinking_mode != "toggle":
        raise ProviderError(
            f"claude config has an unknown openaiThinking {thinking_mode!r}; expected 'toggle'"
        )

    if fold:
        env["AGEDUM_FOLD_SYSTEM_MESSAGES"] = "1"
    if translate:
        env["AGEDUM_TRANSLATE_OPENAI"] = "1"
        if cache_hint:
            env["AGEDUM_OPENAI_PROMPT_CACHE_KEY"] = "1"
        if thinking_mode:
            env["AGEDUM_OPENAI_THINKING"] = thinking_mode

    # Defensive: never let a stray cloud-provider switch leak into the child.
    unset += [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
    ]

    # Escape hatch: arbitrary extra env for the claude child (e.g. CLAUDE_CODE_MAX_OUTPUT_TOKENS,
    # DISABLE_COMPACT) — applied last so it can override the modeled keys above; values stringified.
    extra_env = block.get("extraEnv")
    if isinstance(extra_env, dict):
        for name, value in extra_env.items():
            if value is not None:
                env[str(name)] = str(value)

    return env, unset, ["claude"], ()


# The provider name agedum assigns to a generated custom-endpoint kimi provider in the
# injected kimi config. The user never types it — agedum points kimi at it via --config-file
# and selects the model by its upstream id.
KIMI_PROVIDER_NAME = "agedum"


def _kimi_config_doc(
    block: dict, base_url: str, model: str, secret_env: str, base_env: dict[str, str]
) -> dict:
    """Build a self-sufficient kimi config doc that points at a custom endpoint.

    kimi's config does not interpolate ``$ENV`` and ``--config-file`` replaces the default
    config, so the doc carries the resolved API key verbatim (masked in ``--dry-run``) plus a
    single provider + model; kimi fills every other setting from its ``Config`` defaults.
    """
    provider_type = str(block.get("providerType") or "openai_legacy").strip() or "openai_legacy"
    context_window = block.get("contextWindow")
    max_context_size = (
        int(context_window)
        if isinstance(context_window, (int, float)) and int(context_window) > 0
        else 262144
    )
    capabilities = block.get("capabilities")
    model_capabilities = (
        [str(capability) for capability in capabilities]
        if isinstance(capabilities, list) and capabilities
        else ["thinking"]
    )
    return {
        "default_model": model,
        "models": {
            model: {
                "provider": KIMI_PROVIDER_NAME,
                "model": model,
                "max_context_size": max_context_size,
                "capabilities": model_capabilities,
            }
        },
        "providers": {
            KIMI_PROVIDER_NAME: {
                "type": provider_type,
                "base_url": base_url,
                "api_key": base_env.get(secret_env, ""),
            }
        },
    }


def _kimi_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # kimi's provider/model knobs are appended CLI flags, not env vars. The token
    # (secret_env) reaches the child via the required-env export in build_launch.
    # The CLI binary is overridable: Moonshot's Kimi Code CLI ships as `kimi-cli`, while
    # other packagings expose `kimi`. Default to `kimi` for backward compatibility.
    binary = str(block.get("binary") or "kimi").strip() or "kimi"
    command = [binary]
    model = str(block.get("model") or "").strip()
    base_url = str(block.get("baseUrl") or "").strip()
    config_inline = str(block.get("configInline") or "").strip()
    config_files: list[tuple[str, str, bool]] = []

    if base_url:
        # kimi has no --base-url flag and its config does not interpolate $ENV, so a custom
        # OpenAI-/Anthropic-compatible endpoint becomes a generated kimi config (provider
        # `agedum` + model) with the resolved key baked in — like opencode's
        # OPENCODE_CONFIG_CONTENT — bound into the namespace and loaded via --config-file
        # (which replaces the default config, so the generated doc is self-sufficient).
        if config_inline:
            raise ProviderError(
                "kimi config sets both `baseUrl` and `configInline`; use one — `baseUrl` to "
                "generate a provider config, `configInline` to pass a raw --config string"
            )
        if not model:
            raise ProviderError(
                "kimi config sets `baseUrl` but no `model`; set `model` to the upstream model "
                "id served at that endpoint"
            )
        if not secret_env:
            raise ProviderError("kimi config has a baseUrl but no secretEnv to supply the API key")
        config_doc = _kimi_config_doc(block, base_url, model, secret_env, base_env)
        config_path = str(kimi_config_dir() / "agedum-config.json")
        config_files.append((config_path, json.dumps(config_doc, indent=2) + "\n", False))
        command += ["--config-file", config_path]

    if model:
        command += ["--model", model]
    thinking = block.get("thinking")
    if thinking is True:
        command.append("--thinking")
    elif thinking is False:
        command.append("--no-thinking")
    if block.get("plan") is True:
        command.append("--plan")
    if block.get("yolo") is True:
        command.append("--yolo")
    if config_inline:
        command += ["--config", config_inline]
    return {}, [], command, tuple(config_files)


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


# The provider id agedum stores for a generated custom-endpoint cline launcher. cline ships
# a generic `openai-compatible` provider whose base URL lives only in the stored provider
# config — never on a run flag — so agedum writes a single-provider providers.json and lets
# cline select it via `lastUsedProvider` (see _cline_providers_doc).
CLINE_OPENAI_PROVIDER = "openai-compatible"


def _cline_data_dir(base_url: str, model: str) -> Path:
    """The isolated cline data dir (``CLINE_DATA_DIR``) for one custom-endpoint launcher.

    cline honours a custom base URL only from a *stored* provider and otherwise defaults to
    its own Cline account when that account is configured — so each ``baseUrl`` launcher gets
    its own data dir holding a single ``openai-compatible`` provider (no Cline account to fall
    back to). Derived from the endpoint + model so repeat launches reuse the same dir (and its
    injected skills / session history). Lives under ``~/.cache`` so the conception sandbox's
    writable set already covers cline's own session/db writes.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{base_url}-{model}".lower()).strip("-") or "endpoint"
    return Path.home() / ".cache" / "agedum" / "cline" / slug


def _cline_providers_doc(base_url: str, model: str) -> dict:
    """A single-provider cline ``providers.json`` for a custom OpenAI-compatible endpoint.

    Only structure lives here — provider id, base URL, model. The API key is **not** written:
    it rides the runtime ``--key`` flag (masked in ``--dry-run``), cline's documented
    mechanism, so no secret lands on disk. ``lastUsedProvider`` makes cline select the
    provider without a ``--provider`` flag, which would otherwise rebuild the provider from
    CLI flags and silently drop the stored ``baseUrl`` (posting to the OpenAI default).
    """
    return {
        "version": 1,
        "lastUsedProvider": CLINE_OPENAI_PROVIDER,
        "providers": {
            CLINE_OPENAI_PROVIDER: {
                "settings": {
                    "provider": CLINE_OPENAI_PROVIDER,
                    "apiKey": "",
                    "model": model,
                    "baseUrl": base_url,
                },
                "updatedAt": "2020-01-01T00:00:00.000Z",
                "tokenSource": "manual",
            }
        },
    }


def _cline_auto_approve_flags(block: dict) -> list[str]:
    """``autoApprove`` → cline's ``--auto-approve <boolean>`` (approve every tool call, or
    force-prompt with ``false``). Absent leaves cline's own default (currently auto-approve on)."""
    value = block.get("autoApprove")
    if value is True:
        return ["--auto-approve", "true"]
    if value is False:
        return ["--auto-approve", "false"]
    return []


def _cline_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # Cline's provider/model knobs are appended CLI flags (like kimi). Unlike the other
    # harnesses, Cline takes the token as a per-run flag (`--key`), so the secret lands in
    # argv (visible in the process list while Cline runs) — its documented mechanism, not
    # agedum's choice. build_launch still exports secret_env into the child env via the
    # required-env path, so the value is in Launch.secrets and --dry-run masks it (the
    # command print redacts secret values; Cline is the first harness to put one there).
    model = str(block.get("model") or "").strip()
    effort = str(block.get("effortLevel") or "").strip()
    base_url = str(block.get("baseUrl") or "").strip()

    if base_url:
        # A custom OpenAI-compatible endpoint (Kimi coding subscription, OpenCode-Go, …).
        # cline has no run-time base-URL flag and a `--provider`/`--model` flag set rebuilds
        # the provider from flags — dropping the stored base URL and posting to OpenAI's
        # default. So agedum writes a single-provider providers.json under an isolated
        # CLINE_DATA_DIR and launches with no `--provider`/`--model`; cline selects the stored
        # provider (base URL intact) via `lastUsedProvider`, and the key rides `--key`.
        if not model:
            raise ProviderError(
                "cline config sets `baseUrl` but no `model`; set `model` to the upstream id "
                "served at that endpoint"
            )
        if not secret_env:
            raise ProviderError(
                "cline config sets `baseUrl` but no secretEnv to supply the API key"
            )
        if str(block.get("provider") or "").strip():
            raise ProviderError(
                "cline config sets both `baseUrl` and `provider`; a custom endpoint is reached "
                "through the generated openai-compatible provider, not a named `--provider`"
            )
        data_dir = _cline_data_dir(base_url, model)
        config_path = str(data_dir / "settings" / "providers.json")
        config_doc = json.dumps(_cline_providers_doc(base_url, model), indent=2) + "\n"
        command = ["cline"]
        if effort:
            command += ["--thinking", effort]
        if block.get("plan") is True:
            command.append("--plan")
        command += _cline_auto_approve_flags(block)
        token = base_env.get(secret_env, "")
        if token:
            command += ["--key", token]
        return (
            {"CLINE_DATA_DIR": str(data_dir)},
            [],
            command,
            ((config_path, config_doc, False),),
        )

    command = ["cline"]
    if model:
        command += ["--model", model]
    provider = str(block.get("provider") or "").strip()
    if provider:
        command += ["--provider", provider]
    if effort:
        command += ["--thinking", effort]
    if block.get("plan") is True:
        command.append("--plan")
    command += _cline_auto_approve_flags(block)
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


# The provider name agedum assigns to a generated custom-endpoint pi provider in
# ~/.pi/agent/models.json. The user never types it — agedum selects models as `agedum/<id>`.
PI_PROVIDER_NAME = "agedum"


def _pi_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # pi reads its API key from a conventional env var (ANTHROPIC_API_KEY / OPENAI_API_KEY /
    # DEEPSEEK_API_KEY / GOOGLE_API_KEY / …) — exported via the required-env path in
    # build_launch — so no key flag is appended and no secret lands in argv. provider / model /
    # thinking are CLI flags; a custom endpoint and subagent routing have no flags, so they are
    # generated on-disk config files (the reasonix.toml precedent), merged onto the user's own.
    command = ["pi"]
    model = str(block.get("model") or "").strip()
    subagent_model = str(block.get("subagentModel") or "").strip()
    base_url = str(block.get("baseUrl") or "").strip()
    provider_defs = _provider_defs(block.get("providerDef"))
    config_files: list[tuple[str, str, bool]] = []

    if base_url and provider_defs:
        raise ProviderError(
            "pi config sets both `baseUrl` and `providerDef`; use one — `baseUrl` for a "
            "single inline endpoint, `providerDef` for one or more named providers"
        )

    # The model `subagents.agentOverrides` points every builtin at: the `agedum/<id>` form
    # under a single `baseUrl`, else the verbatim `provider/id` pattern (providerDef / built-in).
    routed_subagent = subagent_model
    model_inputs = _pi_model_inputs(block.get("modelInputs"))
    context_window = _pi_context_window(block.get("contextWindow"))

    if base_url:
        # pi has no --base-url flag: a custom OpenAI-/Anthropic-compatible endpoint becomes a
        # provider named `agedum` in ~/.pi/agent/models.json. The key is referenced by $ENV
        # name (never written), and model selections become `agedum/<id>` so pi routes to it.
        if not model:
            raise ProviderError(
                "pi config sets `baseUrl` but no `model`; set `model` to the upstream model "
                "id served at that endpoint"
            )
        model_ids = _pi_model_ids(model, subagent_model, block.get("models"))
        api = str(block.get("api") or "openai-completions").strip() or "openai-completions"
        models_json = _pi_models_json(
            base_url, api, secret_env, model_ids, model_inputs, context_window
        )
        config_files.append((str(pi_agent_dir() / "models.json"), models_json, True))
        command += ["--model", f"{PI_PROVIDER_NAME}/{model}"]
        if subagent_model:
            routed_subagent = f"{PI_PROVIDER_NAME}/{subagent_model}"
    elif provider_defs:
        # Several named providers in one models.json — e.g. a Kimi executor with DeepSeek-flash
        # subagents (the cross-provider multi-agent case). Each providerDef entry is a provider
        # block; `model` / `subagentModel` are pi `provider/id` patterns referencing them by id,
        # passed through verbatim. Keys are referenced by $ENV name (required_env collects each).
        providers: dict = {}
        for provider_def in provider_defs:
            provider_id, provider_block = _pi_provider_def_block(
                provider_def, model_inputs, context_window
            )
            providers[provider_id] = provider_block
        models_json = json.dumps({"providers": providers}, indent=2) + "\n"
        config_files.append((str(pi_agent_dir() / "models.json"), models_json, True))
        if model:
            command += ["--model", model]
    elif model:
        command += ["--model", model]

    provider = str(block.get("provider") or "").strip()
    if provider:
        command += ["--provider", provider]
    thinking = str(block.get("thinking") or "").strip()
    if thinking:
        command += ["--thinking", thinking]

    # settings.json: a generic `piSettings` passthrough (any settings-based pi extension —
    # `subagents.*`, pi-core keys — deep-merged onto the user's settings.json) plus the
    # `subagentModel` shortcut, composed into ONE fragment so a single settings.json is emitted
    # (two config_files for one target would each merge against the on-disk file, not each
    # other). subagentModel is the baseline (every builtin → one model); an explicit piSettings
    # wins on conflict, so it can override an individual agent or add `subagents.disableBuiltins`.
    settings_fragment: dict = {}
    if subagent_model:
        # pi-subagents reads per-builtin model overrides from settings.json
        # `subagents.agentOverrides` (the opencode-flash / reasonix-subagentModel analog).
        settings_fragment = {
            "subagents": {
                "agentOverrides": {
                    name: {"model": routed_subagent} for name in PI_SUBAGENT_BUILTINS
                }
            }
        }
    pi_settings = block.get("piSettings")
    if pi_settings is not None:
        if not isinstance(pi_settings, dict):
            raise ProviderError("pi `piSettings` must be a JSON object")
        settings_fragment = _deep_merge(settings_fragment, pi_settings)
    if settings_fragment:
        settings_json = json.dumps(settings_fragment, indent=2) + "\n"
        config_files.append((str(pi_agent_dir() / "settings.json"), settings_json, True))

    # piExtensionConfig: an extension whose config is its OWN file under ~/.pi/agent (not
    # settings.json) — e.g. pi-subagents' parallel/async/chain knobs in
    # extensions/subagent/config.json — is reached by a generic relpath→object map, each entry
    # deep-merged onto that file. settings.json / models.json are agedum-managed, so they are
    # rejected here (use piSettings / baseUrl|providerDef).
    config_files += _pi_extension_config_files(block.get("piExtensionConfig"))

    return {}, [], command, tuple(config_files)


def _pi_model_ids(model: str, subagent_model: str, extra: object) -> list[str]:
    """The upstream model ids a custom-endpoint pi provider serves, de-duplicated in stable
    order: the executor ``model``, the ``subagentModel``, then any explicit ``models`` list."""
    ids: list[str] = []
    for value in (model, subagent_model):
        if value and value not in ids:
            ids.append(value)
    if extra is None:
        return ids
    if not isinstance(extra, list):
        raise ProviderError("pi `models` must be a list of model-id strings")
    for item in extra:
        name = str(item or "").strip()
        if name and name not in ids:
            ids.append(name)
    return ids


def _pi_model_inputs(value: object) -> list[str] | None:
    """Validate and return a ``modelInputs`` list (``["text", "image"]``) or ``None``.
    Returns ``None`` when ``value`` is ``None`` (omitted from config) so that the default
    pi behaviour — ``input: ["text"]`` — takes over.  A bare ``[]`` is also treated as
    ``None`` to match the common round-tripped-JSON no-image case."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ProviderError("pi `modelInputs` must be a list of strings")
    cleaned: list[str] = []
    for item in value:
        entry = str(item or "").strip()
        if entry:
            cleaned.append(entry)
    if not cleaned:
        return None
    return cleaned


def _pi_context_window(value: object) -> int | None:
    """Validate and return a ``contextWindow`` value or ``None``.
    Must be a positive integer. Returns ``None`` when omitted so pi's 128k default applies."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProviderError("pi `contextWindow` must be an integer, not a boolean")
    if not isinstance(value, (int, float)):
        raise ProviderError("pi `contextWindow` must be an integer")
    cw = int(value)
    if cw <= 0:
        raise ProviderError("pi `contextWindow` must be positive")
    return cw


def _pi_models_json(
    base_url: str,
    api: str,
    api_key_env: str,
    model_ids: list[str],
    model_inputs: list[str] | None = None,
    context_window: int | None = None,
) -> str:
    """Render the ``~/.pi/agent/models.json`` fragment for the ``agedum`` custom-endpoint
    provider. The API key is referenced by env-var name (``$VAR``), never its value; omitted
    for a keyless endpoint. If ``model_inputs`` is set, it is applied to every model entry.
    If ``context_window`` is set, it is applied to every model entry."""
    provider: dict = {"baseUrl": base_url, "api": api}
    if api_key_env:
        provider["apiKey"] = f"${api_key_env}"
    model_entries: list[dict] = []
    for model_id in model_ids:
        entry: dict = {"id": model_id}
        if model_inputs:
            entry["input"] = model_inputs
        if context_window is not None:
            entry["contextWindow"] = context_window
        model_entries.append(entry)
    provider["models"] = model_entries
    return json.dumps({"providers": {PI_PROVIDER_NAME: provider}}, indent=2) + "\n"


def _pi_provider_def_block(
    provider_def: dict,
    global_model_inputs: list[str] | None = None,
    global_context_window: int | None = None,
) -> tuple[str, dict]:
    """Render one pi ``models.json`` provider entry from a ``providerDef``.

    Fields: ``id`` → the provider name (pi selects models as ``<id>/<model>``), ``baseUrl`` →
    ``baseUrl``, ``model`` → the one upstream model id served there, ``apiKeyEnv`` → ``apiKey``
    as ``$VAR`` (referenced by name, never written; omitted for a keyless endpoint), ``api``
    (default ``openai-completions``). ``id`` / ``baseUrl`` / ``model`` are required. Returns
    ``(id, block)``. If ``global_model_inputs`` is set, it applies to the model entry unless
    the providerDef has its own ``modelInputs``. Same override pattern for ``contextWindow``."""
    fields = {key: str(provider_def.get(key) or "").strip() for key in ("id", "baseUrl", "model")}
    missing = [key for key, value in fields.items() if not value]
    if missing:
        raise ProviderError(f"pi providerDef is missing required field(s): {', '.join(missing)}")
    api = str(provider_def.get("api") or "openai-completions").strip() or "openai-completions"
    block: dict = {"baseUrl": fields["baseUrl"], "api": api}
    api_key_env = str(provider_def.get("apiKeyEnv") or "").strip()
    if api_key_env:
        block["apiKey"] = f"${api_key_env}"
    model_entry: dict = {"id": fields["model"]}
    def_inputs = _pi_model_inputs(provider_def.get("modelInputs"))
    model_inputs = def_inputs if def_inputs is not None else global_model_inputs
    if model_inputs:
        model_entry["input"] = model_inputs
    def_cw = _pi_context_window(provider_def.get("contextWindow"))
    cw = def_cw if def_cw is not None else global_context_window
    if cw is not None:
        model_entry["contextWindow"] = cw
    block["models"] = [model_entry]
    return fields["id"], block


def _pi_extension_warnings(block: dict) -> list[str]:
    """Advisories for pi extensions a config relies on but the host hasn't installed.

    ``requireExtensions`` (a string or list) names extensions the provider needs; **pi-subagents
    is implicitly required** when ``subagentModel`` or a ``piSettings.subagents`` block is set
    (those `subagents.*` settings are inert without the extension). Each is matched against the
    host's installed packages (``~/.pi/agent/settings.json`` `packages` + the
    ``~/.pi/agent/npm/node_modules`` dir). A missing one yields a warning — or, when the config
    sets ``strict: true``, a fail-loud :class:`ProviderError` (so a task / CI run refuses rather
    than silently degrading, e.g. to a single agent). agedum never installs (a host action)."""
    require = block.get("requireExtensions")
    specs = [require] if isinstance(require, str) else require if isinstance(require, list) else []
    required: list[str] = []
    for spec in specs:
        name = _pi_pkg_name(str(spec))
        if name and name not in required:
            required.append(name)
    pi_settings = block.get("piSettings")
    needs_subagents = bool(str(block.get("subagentModel") or "").strip()) or (
        isinstance(pi_settings, dict) and "subagents" in pi_settings
    )
    if needs_subagents and "pi-subagents" not in required:
        required.append("pi-subagents")
    if not required:
        return []

    installed = _pi_installed_package_names()
    missing = [name for name in required if name not in installed]
    if not missing:
        return []
    messages = [
        f"pi extension '{name}' is required by this provider but is not installed on the host; "
        f"run `pi install npm:{name}` (its config is inert without it)"
        for name in missing
    ]
    if block.get("strict") is True:
        raise ProviderError("; ".join(messages))
    return messages


def _pi_pkg_name(spec: str) -> str:
    """The bare package name from an extension spec: ``npm:pi-subagents`` / ``git:…/pi-foo`` /
    ``pi-foo`` → ``pi-foo``. Empty input → ``""``."""
    value = str(spec or "").strip()
    for scheme in ("npm:", "git:", "file:"):
        if value.startswith(scheme):
            value = value[len(scheme) :]
            break
    return value.rstrip("/").split("/")[-1]


def _pi_installed_package_names() -> set[str]:
    """Bare names of pi extensions installed on the host — from ``~/.pi/agent/settings.json``
    `packages` and the ``~/.pi/agent/npm/node_modules`` directory. Best-effort: an unreadable
    settings.json or absent node_modules simply yields fewer names."""
    agent_dir = pi_agent_dir()
    names: set[str] = set()
    try:
        settings = json.loads((agent_dir / "settings.json").read_text())
        if isinstance(settings, dict):
            for spec in settings.get("packages") or []:
                name = _pi_pkg_name(str(spec))
                if name:
                    names.add(name)
    except (OSError, ValueError):
        pass
    try:
        for child in (agent_dir / "npm" / "node_modules").iterdir():
            if child.is_dir():
                names.add(child.name)
    except OSError:
        pass
    return names


def _pi_extension_config_files(value: object) -> list[tuple[str, str, bool]]:
    """Generated config files for ``piExtensionConfig`` — a ``{relpath: object}`` map writing
    arbitrary JSON under ``~/.pi/agent`` (deep-merged onto any existing file), for an extension
    whose config is its **own file** rather than ``settings.json`` (e.g. pi-subagents'
    ``parallel``/``async``/``chain`` in ``extensions/subagent/config.json``).

    Each key is a path **relative to** ``~/.pi/agent`` (not absolute, no ``..`` — the file must
    stay under the agent dir). The agedum-managed ``settings.json`` / ``models.json`` are
    rejected (use ``piSettings`` / ``baseUrl``|``providerDef``) so one target is never written
    by two config files."""
    if value is None:
        return []
    if not isinstance(value, dict):
        raise ProviderError(
            "pi `piExtensionConfig` must be a JSON object mapping a relative path to a config "
            "object"
        )
    managed = {"settings.json": "piSettings", "models.json": "baseUrl / providerDef"}
    files: list[tuple[str, str, bool]] = []
    for rel, content in value.items():
        rel_path = str(rel).strip()
        candidate = Path(rel_path) if rel_path else Path()
        if not rel_path or candidate.is_absolute() or ".." in candidate.parts:
            raise ProviderError(
                f"pi `piExtensionConfig` key {rel!r} must be a relative path under ~/.pi/agent "
                "(not absolute, no '..')"
            )
        norm = candidate.as_posix()
        if norm in managed:
            raise ProviderError(
                f"pi `piExtensionConfig` cannot target {norm!r} (agedum-managed); "
                f"use `{managed[norm]}` instead"
            )
        if not isinstance(content, dict):
            raise ProviderError(f"pi `piExtensionConfig` value for {rel!r} must be a JSON object")
        files.append((str(pi_agent_dir() / candidate), json.dumps(content, indent=2) + "\n", True))
    return files


def merge_json_onto_file(target: Path, fragment: str) -> str:
    """Deep-merge an agedum-generated JSON ``fragment`` onto the existing JSON at ``target``.

    Returns the merged document as text (2-space indent). When ``target`` is absent,
    unreadable, not JSON, or not a JSON object, the fragment is returned verbatim — a malformed
    or missing user file never blocks the launch, and the agedum keys still land. This lets an
    injected user-scope config (pi's ``models.json`` / ``settings.json``) augment rather than
    mask the user's own.
    """
    new = json.loads(fragment)
    try:
        existing = json.loads(target.read_text())
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict) and isinstance(new, dict):
        return json.dumps(_deep_merge(existing, new), indent=2) + "\n"
    return fragment


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
    return {}, [], command, (("reasonix.toml", toml, False),)


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
    """Escape a string for a TOML double-quoted basic string: backslash, quote, and the
    control characters a basic string may not carry raw (``\\n`` / ``\\t`` / ``\\r``;
    anything else below 0x20 as ``\\uXXXX``) — so no input can emit invalid TOML."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return "".join(f"\\u{ord(char):04X}" if ord(char) < 0x20 else char for char in escaped)


def _toml_scalar(value: object) -> str:
    """Render a JSON scalar as a TOML value for a codex ``-c key=<value>`` override: a bool as
    ``true`` / ``false``, an int/float bare, everything else as a quoted basic string. codex
    parses each ``-c`` value as TOML, so a context window must go bare (``262144``) and a flag
    bare (``true``) — quoting them would type-mismatch the setting."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f'"{_toml_escape(str(value))}"'


def _codex_debug_models() -> dict | None:
    """codex's resolved model catalog (``codex debug models``), or ``None`` if unavailable.

    Runs the installed codex to read its **own** bundled catalog — the source of a
    version-correct template entry (``base_instructions`` and capability flags) to clone for a
    custom model. Any failure (codex absent, non-zero exit, unparseable / empty output) returns
    ``None`` so the caller degrades to codex's fallback metadata instead of failing the launch.
    """
    try:
        result = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("models"), list) and data["models"]:
        return data
    return None


def _codex_synthesized_catalog(model: str, catalog: dict, overrides: dict) -> str:
    """A one-entry ``model_catalog_json`` document for ``model``, cloned from ``catalog``'s first
    entry so codex recognises the custom model (silencing the "metadata not found" warning and
    driving the context-usage meter from ``context_window``).

    The template supplies the version-correct required fields (chiefly ``base_instructions``);
    ``overrides`` sets the values codex can't infer — ``contextWindow`` →
    ``context_window`` + ``max_context_window``, plus optional ``displayName`` / ``description``.
    """
    entry = json.loads(json.dumps(catalog["models"][0]))
    entry["slug"] = model
    entry["display_name"] = str(overrides.get("displayName") or model)
    if overrides.get("description"):
        entry["description"] = str(overrides["description"])
    context_window = overrides.get("contextWindow")
    if isinstance(context_window, int) and not isinstance(context_window, bool):
        entry["context_window"] = context_window
        entry["max_context_window"] = context_window
    entry["visibility"] = "list"
    # Drop template UI hooks that would misfire for the cloned model.
    entry.pop("availability_nux", None)
    entry.pop("upgrade", None)
    return json.dumps({"models": [entry]}, separators=(",", ":"))


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
    if options:
        # The default-model options hang off `provider.<id>.models.<id>.options`, which
        # needs a `provider/model`-shaped `model` to address. Silently dropping them
        # would read as "configured" while doing nothing — fail loudly instead.
        if not model or "/" not in model:
            raise ProviderError(
                "opencode `effortLevel`/`defaultOptions` need `model` in `provider/model` "
                f"form to attach to (got {model!r})"
            )
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


# The provider id agedum assigns to a custom-endpoint codex provider, passed via
# `-c model_provider=…` / `-c model_providers.<id>.…`. Must not collide with codex's reserved
# built-in ids (openai / ollama / lmstudio / amazon-bedrock); the user never types it.
CODEX_PROVIDER_NAME = "agedum"


def _codex_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # codex selects its model and provider from CLI flags, so an endpoint is passed via
    # repeatable `-c key=value` overrides (codex parses each value as TOML), winning over
    # ~/.codex/config.toml. The API key reaches codex by its conventional env-var name (the
    # provider's `secretEnv`, referenced as the provider's `env_key`) via the required-env
    # export in build_launch — never written to a file or argv.
    env: dict[str, str] = {}
    command = ["codex"]
    model = str(block.get("model") or "").strip()
    base_url = str(block.get("baseUrl") or "").strip()
    # Recent codex has REMOVED Chat Completions support (`wire_api = "chat"` is rejected); it
    # speaks only the Responses API. `chatCompletions: true` declares the endpoint is
    # Chat-Completions-only (e.g. DeepSeek direct), so agedum interposes a Responses↔Chat
    # translation proxy at launch (AGEDUM_CODEX_CHAT_UPSTREAM signals the CLI; see
    # cli.main._maybe_codex_proxy). codex then speaks Responses to the proxy — no wire_api
    # override — and the `base_url` below is rewritten to the proxy address at launch.
    chat_completions = block.get("chatCompletions") is True

    if base_url:
        # A custom endpoint becomes a provider named `agedum`, selected with
        # `-c model_provider=agedum`. The key is referenced by env_key NAME, never its value.
        overrides = [
            ("model_provider", CODEX_PROVIDER_NAME),
            (f"model_providers.{CODEX_PROVIDER_NAME}.name", CODEX_PROVIDER_NAME),
            (f"model_providers.{CODEX_PROVIDER_NAME}.base_url", base_url),
        ]
        if chat_completions:
            env["AGEDUM_CODEX_CHAT_UPSTREAM"] = base_url
        else:
            # A real Responses endpoint: emit wire_api only when explicitly set (else codex's
            # own default applies).
            wire_api = str(block.get("wireApi") or "").strip()
            if wire_api:
                overrides.append((f"model_providers.{CODEX_PROVIDER_NAME}.wire_api", wire_api))
        if secret_env:
            overrides.append((f"model_providers.{CODEX_PROVIDER_NAME}.env_key", secret_env))
        for key, value in overrides:
            command += ["-c", f'{key}="{_toml_escape(value)}"']

    if model:
        command += ["-m", model]

    # `codexConfig` is a passthrough of arbitrary codex config keys as `-c key=<toml>` overrides
    # (they win over ~/.codex/config.toml). It carries the model metadata codex can't learn from
    # a translated custom endpoint — `model_context_window` (the context-meter denominator, since
    # the /models probe is answered empty) and `model_supports_reasoning_summaries` /
    # `model_reasoning_summary` (so codex renders the reasoning the Responses↔Chat proxy surfaces).
    codex_config = block.get("codexConfig")
    if codex_config is not None:
        if not isinstance(codex_config, dict):
            raise ProviderError("codex config `codexConfig` must be a table of key → value")
        for key, value in codex_config.items():
            command += ["-c", f"{key}={_toml_scalar(value)}"]

    # codex has no global "route subagents to a fast model" knob (openai/codex#19482) and no
    # inline agent config — custom agents are standalone TOML files under ~/.codex/agents/
    # (personal) or .codex/agents/ (project) the primary delegates to on explicit request. So
    # agedum binds agent source files into those dirs: `subagentModel` is sugar for one fast
    # `flash` worker; `codexAgents` / `codexProjectAgents` bind every *.toml in a source dir.
    # All are INERT unless the primary is asked to spawn them; see docs/harnesses/codex.md.
    config_files: list[tuple[str, str, bool]] = []
    seen_targets: set[str] = set()

    def _add_agent(target: str, content: str) -> None:
        if target in seen_targets:
            raise ProviderError(f"duplicate codex agent target {target!r}")
        seen_targets.add(target)
        config_files.append((target, _render_codex_agent(content), False))

    subagent_model = str(block.get("subagentModel") or "").strip()
    if subagent_model:
        _add_agent(
            str(codex_config_dir() / "agents" / "flash.toml"),
            _codex_flash_agent_toml(subagent_model),
        )
    for source in _codex_agent_sources(block.get("codexAgents"), "codexAgents"):
        _add_agent(str(codex_config_dir() / "agents" / source.name), source.read_text())
    for source in _codex_agent_sources(block.get("codexProjectAgents"), "codexProjectAgents"):
        _add_agent(f".codex/agents/{source.name}", source.read_text())

    # `codexModelCatalog` teaches codex about a custom model it doesn't ship in its bundled
    # catalog: agedum clones a real catalog entry (via `codex debug models`, for version-correct
    # `base_instructions`) as this `model`, applying the table's overrides (chiefly
    # `contextWindow`), writes it to a generated `model_catalog_json` file, and points codex at
    # it. That silences the "metadata not found" warning and drives the context-usage meter. If
    # codex can't be queried, the catalog is skipped and codex uses its fallback metadata — the
    # launch never fails on it.
    catalog_config = block.get("codexModelCatalog")
    if catalog_config is not None:
        if not isinstance(catalog_config, dict):
            raise ProviderError("codex config `codexModelCatalog` must be a table")
        if not model:
            raise ProviderError("codex config `codexModelCatalog` needs a `model` to name")
        catalog = _codex_debug_models()
        if catalog is not None:
            target = str(codex_config_dir() / "agedum-model-catalog.json")
            content = _codex_synthesized_catalog(model, catalog, catalog_config)
            if target in seen_targets:
                raise ProviderError(f"duplicate codex config target {target!r}")
            seen_targets.add(target)
            config_files.append((target, content, False))
            command += ["-c", f'model_catalog_json="{_toml_escape(target)}"']

    return env, [], command, tuple(config_files)


# codex custom agents inherit this sandbox unless their source sets `sandbox_mode`. agedum
# launches are write-confined (the bwrap mount namespace + the conception-sandbox base), so
# `workspace-write` is the codex mode that matches: codex may write the workspace, the mount
# confines it to the sandbox's readWrite set.
DEFAULT_CODEX_AGENT_SANDBOX_MODE = "workspace-write"


def _codex_agent_sources(value: object, key: str) -> list[Path]:
    """Resolve a ``codexAgents`` / ``codexProjectAgents`` directory ref to its ``*.toml`` files.

    ``value`` is a directory path anchored at the providers root (or absolute when it starts
    with ``/``); every ``*.toml`` directly inside it is an agent source, returned sorted by
    name. ``None`` → no sources. Raises :class:`ProviderError` when set to a non-string or to a
    path that is not an existing directory.
    """
    if value is None:
        return []
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"`{key}` must be a non-empty directory path string")
    ref = value.strip()
    directory = (Path(ref) if ref.startswith("/") else providers_dir() / ref).expanduser()
    if not directory.is_dir():
        raise ProviderError(f"`{key}` {ref!r} is not a directory (resolved {directory})")
    return sorted(directory.glob("*.toml"))


def _render_codex_agent(content: str) -> str:
    """Return a codex custom-agent TOML, injecting agedum's default ``sandbox_mode`` when the
    source omits it; an explicit ``sandbox_mode`` is passed through unchanged.

    The check is a flat-key line scan — agent TOMLs are flat tables, so it does not parse
    nested structure. The source must end as valid TOML (all blocks closed) for the appended
    top-level key to stay valid.
    """
    if _toml_sets_key(content, "sandbox_mode"):
        return content
    body = content if content.endswith("\n") else content + "\n"
    return f'{body}sandbox_mode = "{DEFAULT_CODEX_AGENT_SANDBOX_MODE}"\n'


def _toml_sets_key(content: str, key: str) -> bool:
    """True when flat TOML ``content`` assigns top-level ``key`` (a ``key = ...`` line)."""
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(key) and stripped[len(key) :].lstrip().startswith("="):
            return True
    return False


def _codex_flash_agent_toml(model: str) -> str:
    """A codex custom-agent definition (``~/.codex/agents/flash.toml``) the primary can delegate
    to: a fast, low-cost worker running ``model``.

    codex custom agents are standalone TOML files keyed by ``name``; ``description`` and
    ``developer_instructions`` are required, ``model`` overrides the parent session's model.
    The ``model`` id is the only dynamic value (escaped); the rest is a fixed template, rendered
    through :func:`_render_codex_agent` (which adds the default sandbox) by the caller.
    """
    return (
        'name = "flash"\n'
        'description = "Fast, low-cost worker for routine, well-scoped subtasks. '
        'Delegate mechanical work here to keep the primary model free for harder reasoning."\n'
        'developer_instructions = """\n'
        "You are a fast, cost-efficient worker. Carry out the delegated subtask directly and "
        "return a concise, complete result; prefer doing the work over deliberating.\n"
        '"""\n'
        f'model = "{_toml_escape(model)}"\n'
    )
