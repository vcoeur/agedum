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

# Sentinel baked into an opencode ``providerDef``'s rendered ``options.apiKey``.
# The generated wrapper replaces it with the *value* of the provider's API-key env
# var at run time (bash parameter expansion), because opencode's ``{env:…}``
# substitution is unreliable for a custom provider's ``options.apiKey``. agedum
# never sees the token — the shell does the splice.
OPENCODE_APIKEY_PLACEHOLDER = "__AGEDUM_OPENCODE_APIKEY__"


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
    # An opencode providerDef's key env var is validated + exported even if the
    # author forgot to list it, so the run-time apiKey splice always has a value.
    block = config.get("config")
    if isinstance(block, dict):
        provider_def = block.get("providerDef")
        if isinstance(provider_def, dict):
            api_key_env = str(provider_def.get("apiKeyEnv") or "").strip()
            if api_key_env and api_key_env not in result:
                result.append(api_key_env)
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
        doc_json = json.dumps(document, sort_keys=True)
        api_key_env = _opencode_provider_def(block)[3] if block.get("providerDef") else ""
        if api_key_env:
            exports += _opencode_config_runtime_key(doc_json, api_key_env)
        else:
            exports.append(_export_lit("OPENCODE_CONFIG_CONTENT", doc_json))
    return exports, ["opencode"]


def _opencode_provider_def(block: dict) -> tuple[str, str, str, str, str]:
    """Validate and unpack a ``providerDef``: (id, npm, baseUrl, apiKeyEnv, name).

    Raises :class:`BuildScriptError` when a required field is missing.
    """
    provider_def = block.get("providerDef") or {}
    if not isinstance(provider_def, dict):
        raise BuildScriptError("`providerDef` must be a JSON object")
    fields = {
        "id": str(provider_def.get("id") or "").strip(),
        "npm": str(provider_def.get("npm") or "").strip(),
        "baseUrl": str(provider_def.get("baseUrl") or "").strip(),
        "apiKeyEnv": str(provider_def.get("apiKeyEnv") or "").strip(),
    }
    missing = [key for key, value in fields.items() if not value]
    if missing:
        raise BuildScriptError(f"providerDef is missing required field(s): {', '.join(missing)}")
    name = str(provider_def.get("name") or "").strip()
    return fields["id"], fields["npm"], fields["baseUrl"], fields["apiKeyEnv"], name


def _opencode_config_runtime_key(doc_json: str, api_key_env: str) -> list[str]:
    """Emit the shell that exports ``OPENCODE_CONFIG_CONTENT`` with the provider key
    spliced in at run time.

    The config JSON carries :data:`OPENCODE_APIKEY_PLACEHOLDER` where the key goes;
    bash parameter expansion replaces it with ``$<api_key_env>`` (already validated
    and exported by the required-env block). agedum never holds the token.
    """
    tmp = "__agedum_oc_config"
    expansion = "${" + tmp + "/" + OPENCODE_APIKEY_PLACEHOLDER + "/${" + api_key_env + "}}"
    return [
        f"{tmp}={shlex.quote(doc_json)}",
        f'export OPENCODE_CONFIG_CONTENT="{expansion}"',
        f"unset {tmp}",
    ]


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

    # An explicit provider definition (npm + baseURL + run-time-injected apiKey),
    # deep-merged so it coexists with any per-model reasoning options above.
    if block.get("providerDef"):
        def_id, npm, base_url, _api_key_env, name = _opencode_provider_def(block)
        entry: dict = {
            "npm": npm,
            "options": {"baseURL": base_url, "apiKey": OPENCODE_APIKEY_PLACEHOLDER},
        }
        if name:
            entry["name"] = name
        providers = document.setdefault("provider", {})
        providers[def_id] = _deep_merge(providers.get(def_id, {}), entry)

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
