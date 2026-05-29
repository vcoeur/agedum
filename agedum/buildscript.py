"""Generate a provider-wrapper shell script from an agent config JSON.

``agedum --build-script conf.json [out.sh]`` compiles a condash-style provider
config into a standalone bash wrapper that, at run time:

1. sources a ``.env`` (``${AGENTS_ENV_FILE:-$HOME/.config/agents/.env}``),
2. validates + exports every variable named in the config's ``requiredEnv``,
3. exports the provider/model/auth environment for the harness, then
4. ``exec``s ``agedum --wrapper <harness> -- <harness> "$@"`` — composing this
   codegen mode with the virtual-FS injection mode.

agedum itself never reads a token at run time; all secret handling lives in the
generated shell. The per-harness env mapping mirrors condash's pre-4.0 agent
launcher (``buildClaudeSpawn`` / ``buildKimiSpawn`` / ``buildOpencodeSpawn``).
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

HARNESSES = ("claude", "kimi", "opencode")

# opencode's built-in agent names keep opencode's own mode; ``primary`` only
# applies to custom agents.
OPENCODE_BUILTINS = frozenset({"build", "plan", "general", "explore", "scout"})


class BuildScriptError(RuntimeError):
    """The config could not be compiled into a wrapper script."""


def build_script_from_file(path: Path) -> str:
    """Load a provider config JSON from `path` and return the wrapper script text."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise BuildScriptError(f"cannot read config {path}: {exc}") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BuildScriptError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise BuildScriptError(f"config {path} must be a JSON object, not {type(config).__name__}")
    return build_script(config)


def build_script(config: dict) -> str:
    """Compile a parsed provider config into wrapper-script text.

    The harness is read from the config's ``harness`` field (the config file is the
    build input). Raises :class:`BuildScriptError` on an unknown harness or bad shape.
    """
    harness = config.get("harness")
    if harness not in HARNESSES:
        raise BuildScriptError(
            f"unsupported or missing harness {harness!r}; expected one of {', '.join(HARNESSES)}"
        )
    label = str(config.get("slug") or config.get("name") or harness)
    block = config.get("config") or {}
    if not isinstance(block, dict):
        raise BuildScriptError("`config` must be a JSON object")
    secret_env = str(config.get("secretEnv") or "").strip()
    required = _required_env(config, secret_env)

    builders = {"claude": _claude_body, "kimi": _kimi_body, "opencode": _opencode_body}
    exports, command = builders[harness](block, secret_env)
    return _assemble(label, harness, required, exports, command)


def _required_env(config: dict, secret_env: str) -> list[str]:
    """The env vars the wrapper validates + exports: the declared ``requiredEnv``
    list, with ``secretEnv`` appended if not already present. Order is stable."""
    declared = config.get("requiredEnv")
    result: list[str] = []
    if isinstance(declared, list):
        for value in declared:
            name = str(value).strip()
            if name and name not in result:
                result.append(name)
    if secret_env and secret_env not in result:
        result.append(secret_env)
    return result


# ---------------------------------------------------------------------------
# per-harness env/command builders -> (export_lines, command_tokens)
# ---------------------------------------------------------------------------


def _claude_body(block: dict, secret_env: str) -> tuple[list[str], list[str]]:
    base_url = str(block.get("baseUrl") or "").strip()
    if not base_url:
        # Native Claude: no provider overrides (the all-empty config). Run bare.
        return [], ["claude"]
    if not secret_env:
        raise BuildScriptError(
            "claude config has a baseUrl but no secretEnv to supply the API token"
        )

    exports = [_export_lit("ANTHROPIC_BASE_URL", base_url)]
    if str(block.get("authStyle") or "bearer").strip() == "apikey":
        exports.append(_export_ref("ANTHROPIC_API_KEY", secret_env))
        exports.append("unset ANTHROPIC_AUTH_TOKEN")
    else:
        exports.append(_export_ref("ANTHROPIC_AUTH_TOKEN", secret_env))
        exports.append("unset ANTHROPIC_API_KEY")

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
            exports.append(_export_lit(var, value))

    max_tokens = block.get("maxContextTokens") or 0
    if isinstance(max_tokens, (int, float)) and int(max_tokens) > 0:
        exports.append(_export_lit("CLAUDE_CODE_MAX_CONTEXT_TOKENS", str(int(max_tokens))))

    effort = str(block.get("effortLevel") or "").strip()
    if effort:
        exports.append(_export_lit("CLAUDE_CODE_EFFORT_LEVEL", effort))

    for key, var in (
        ("disableCaching", "DISABLE_PROMPT_CACHING"),
        ("disable1M", "CLAUDE_CODE_DISABLE_1M_CONTEXT"),
        ("disableAdaptiveThinking", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"),
        ("disableTelemetry", "DISABLE_TELEMETRY"),
        ("disableErrorReporting", "DISABLE_ERROR_REPORTING"),
        ("disableClaudeApiSkill", "CLAUDE_CODE_DISABLE_CLAUDE_API_SKILL"),
    ):
        if block.get(key) is True:
            exports.append(_export_lit(var, "1"))

    # Strict Anthropic-compat endpoints (e.g. DeepSeek's /anthropic) reject a `system`
    # role inside `messages[]`. This flag makes agedum's wrapper interpose a local proxy
    # that folds those entries into the top-level `system` field — see agedum.proxy.
    if block.get("foldSystemMessages") is True:
        exports.append(_export_lit("AGEDUM_FOLD_SYSTEM_MESSAGES", "1"))

    # Defensive: never let a stray cloud-provider switch leak into the child.
    for var in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
    ):
        exports.append(f"unset {var}")

    return exports, ["claude"]


def _kimi_body(block: dict, secret_env: str) -> tuple[list[str], list[str]]:
    # kimi's provider/model knobs are appended CLI flags, not env vars. The token
    # (secret_env) reaches the child via the requiredEnv export in _assemble.
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
    return [], command


def _opencode_body(block: dict, secret_env: str) -> tuple[list[str], list[str]]:
    exports: list[str] = []
    if block.get("disableExternalSkills") is True:
        exports.append(_export_lit("OPENCODE_DISABLE_EXTERNAL_SKILLS", "1"))
    document = _opencode_config_doc(block)
    if document:
        exports.append(_export_lit("OPENCODE_CONFIG_CONTENT", json.dumps(document, sort_keys=True)))
    return exports, ["opencode"]


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

    extra = block.get("extraConfigJson")
    if isinstance(extra, str) and extra.strip():
        try:
            merged = json.loads(extra)
        except json.JSONDecodeError as exc:
            raise BuildScriptError(f"extraConfigJson is not valid JSON: {exc}") from exc
        if isinstance(merged, dict):
            document = _deep_merge(document, merged)

    return document


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


# ---------------------------------------------------------------------------
# assembly + shell quoting
# ---------------------------------------------------------------------------


def _assemble(
    label: str, harness: str, required: list[str], exports: list[str], command: list[str]
) -> str:
    """Stitch the header, env block, exports, and exec line into a complete script."""
    lines = [
        "#!/usr/bin/env bash",
        f"# Generated by `agedum --build-script` from provider {shlex.quote(label)}.",
        "# Do not edit by hand — regenerate from the provider config JSON.",
        "set -euo pipefail",
        "",
    ]

    if required:
        lines += [
            'env_file="${AGENTS_ENV_FILE:-$HOME/.config/agents/.env}"',
            'if [[ ! -f "$env_file" ]]; then',
            f'  echo {shlex.quote(f"{label}: env file not found:")} "$env_file" >&2',
            "  exit 1",
            "fi",
            "# shellcheck source=/dev/null",
            'source "$env_file"',
        ]
        for var in required:
            message = f"{var} is required by provider {label} but is not set"
            lines.append(f'export {var}="${{{var}:?{message}}}"')
        lines.append("")

    if exports:
        lines += exports
        lines.append("")

    exec_command = " ".join(shlex.quote(token) for token in command)
    lines.append(f'exec agedum --wrapper {harness} -- {exec_command} "$@"')
    return "\n".join(lines) + "\n"


def _export_lit(var: str, value: str) -> str:
    """`export VAR=<shell-quoted literal>`."""
    return f"export {var}={shlex.quote(value)}"


def _export_ref(var: str, ref: str) -> str:
    """`export VAR="$REF"` — assign one (already-validated) env var to another."""
    return f'export {var}="${ref}"'
