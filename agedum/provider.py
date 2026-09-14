"""Resolve a provider config (JSON or YAML) into the launch environment and command.

``agedum <name|path>`` reads a condash-style provider config and computes, at run
time: the variables to export/unset and the base command for the harness. The
per-harness mapping mirrors condash's pre-4.0 agent launcher (``buildClaudeSpawn`` /
``buildKimiSpawn`` / ``buildOpencodeSpawn``).

A config may be **JSON** (the legacy format, loaded exactly as always) or **YAML**
(a document declaring the envelope version, ``schema: agedum-provider/v1``). YAML is
parsed, not translated: it yields the same in-memory dict the equivalent JSON would,
and no file is ever converted.

Unlike the retired ``--build-script`` codegen — which emitted a shell wrapper that
sourced the ``.env`` itself, so agedum never saw a token — this path reads the env
file (``${AGENTS_ENV_FILE:-~/.config/agents/.env}``) into the agedum process and sets
the resolved values in the child environment.

Resolution: ``agedum <value>`` where ``value`` is a **path** (it contains ``/`` or a
recognised config extension; absolute as-is, else relative to CWD) or a **provider
name** (resolved under ``${AGENTS_PROVIDERS_DIR:-~/.config/agents/providers}``). A ref
with no recognised extension tries ``.json``, then ``.yaml``, then ``.yml``; an explicit
``.json`` that does not exist falls back to its ``.yaml`` sibling, so converted YAML
bases keep their old JSON referrers working. The same rule resolves the ``agedum
<value>`` argument, an ``extends`` reference, and an ``include`` reference.

Composition: a config may **``include``** one or more fragment configs (a string or list,
resolved like ``extends``) — pure composition, not inheritance. One file's effective
config merges most-default first: every include target (recursively resolved),
deep-merged left→right; then the ``extends`` chain, whose keys beat an included
fragment's on conflict; then the file's own keys. ``requiredEnv`` unions across all
three layers. ``include`` is a meta key like ``extends`` — consumed during resolution,
never present in the merged result.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

from agedum.harness import Sandbox, codex_config_dir, kimi_config_dir, pi_agent_dir
from agedum.proxy import OPENAI_CODEX_UPSTREAM, failover_route_base

HARNESSES = ("claude", "kimi", "opencode", "cline", "reasonix", "aider", "pi", "codex")

# The envelope-version keys a YAML provider config may declare. JSON configs are the
# legacy format and need no key — they load exactly as they always have and carry
# v1 semantics forever (v2 is YAML-only).
PROVIDER_SCHEMA_KEY = "schema"
PROVIDER_SCHEMA_VERSION = "agedum-provider/v1"
PROVIDER_SCHEMA_VERSION_2 = "agedum-provider/v2"
PROVIDER_SCHEMA_VERSIONS = (PROVIDER_SCHEMA_VERSION, PROVIDER_SCHEMA_VERSION_2)

# The run-time model catalogue: a fixed-name YAML document at the providers root
# (or the file a config's `modelsCatalog` ref points at) holding verbatim
# per-model fragments opencode configs reach through `modelRef`. YAML-only in v1.
MODEL_CATALOGUE_NAME = "models.yaml"
MODEL_CATALOGUE_SCHEMA_VERSION = "agedum-models/v1"

# The suffixes a config file may carry. ``.yaml`` / ``.yml`` both select the YAML
# reader; every other suffix (``.json`` included, and none at all) selects JSON.
YAML_SUFFIXES = (".yaml", ".yml")
JSON_SUFFIX = ".json"

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
# config_files is a tuple of (target, content, merge_json[, writable]) entries the launcher
# writes into the namespace: `target` is project-root-relative (reasonix's reasonix.toml) or
# absolute (pi's user-scope ~/.pi/agent/models.json + settings.json); `merge_json` deep-merges
# the content onto any existing JSON file at the target. The optional 4th field `writable`
# (default False) seeds the file **directly into its target dir** — which must already be a
# writable sandbox dir — instead of read-only binding it, for a tool that rewrites the file
# itself (cline persists its provider selection to providers.json, which a ro-bind makes fail
# with EROFS). Empty for every harness without a generated on-disk config.
ConfigFile = tuple[str, str, bool] | tuple[str, str, bool, bool]
BuilderResult = tuple[dict[str, str], list[str], list[str], tuple[ConfigFile, ...]]


class ProviderError(RuntimeError):
    """A provider config could not be resolved into a launch."""


class ProviderSchemaError(ProviderError):
    """A YAML provider config is missing or carries an unsupported ``schema`` version."""


class ModelCatalogSchemaError(ProviderError):
    """A model catalogue is missing or carries an unsupported ``schema`` version."""


class ExpansionError(ProviderError):
    """A v2 config's expansion intent could not be resolved against the catalogue."""


class YamlBooleanTrapError(ProviderError):
    """A YAML config has an unquoted on/off/yes/no where the envelope wants a string."""


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
    config_files: tuple[ConfigFile, ...] = ()
    warnings: tuple[str, ...] = ()
    sandbox: Sandbox | None = None


class FailoverPlan(NamedTuple):
    """The live failover proxy's handle, baked into the emitted opencode config.

    ``base_url`` is the running :class:`agedum.proxy.FailoverProxy`'s ephemeral
    address; ``routes`` are the provider ids whose ``options.baseURL`` the config
    builder rewrites to ``<base_url>/oc/<id>`` (the built-in ``openai`` provider among
    them when mapped). ``None`` everywhere means no failover — the rollback switch.
    """

    base_url: str
    routes: tuple[str, ...]


def failover_spec(config: dict, base_env: dict[str, str]) -> tuple[dict | None, list[str]]:
    """Parse + validate a launcher's top-level ``failover`` block into the proxy spec.

    Returns ``(spec, warnings)``; ``(None, [])`` when the block is absent. Raises
    :class:`ProviderError` on an invalid block — a bad chain must fail the launch, not
    silently degrade to no failover. The spec shape is what
    :class:`agedum.proxy.FailoverProxy` consumes:

    - ``routes`` — per provider id: the upstream base URL, the resolved API key, the
      model catalogue (``id`` override + ``options`` per model key) and the wire-id
      reverse map. Built from the launcher's ``providerDef`` list plus the built-in
      ``openai`` OAuth route (D1 PASS: verbatim forwarding to the codex endpoint);
      model keys the catalogue doesn't declare are seeded from the agents' ``model``
      references (openai's models live in opencode's own registry, not the config).
    - ``status`` / ``messages`` / ``max_walk`` / ``vision`` / ``chains`` /
      ``rung_options`` — straight from the block, with openai rungs pruned (D4: not a
      fallback target in v1 — the OAuth bearer only arrives on openai primaries; a pruned
      rung is a warning) and duplicate runtime rungs deduped (first occurrence wins).
    """
    block = config.get("failover")
    if not block:
        return None, []
    if config.get("harness") != "opencode":
        raise ProviderError("`failover` is only implemented for the opencode harness")
    if not isinstance(block, dict):
        raise ProviderError("`failover` must be a JSON object")

    block_cfg = config.get("config") or {}
    routes: dict[str, dict] = {}
    for provider_def in _provider_defs(block_cfg.get("providerDef")):
        provider_id = str(provider_def.get("id") or "").strip()
        base_url = str(provider_def.get("baseUrl") or "").strip()
        api_key_env = str(provider_def.get("apiKeyEnv") or "").strip()
        fields = (("id", provider_id), ("baseUrl", base_url), ("apiKeyEnv", api_key_env))
        missing = [name for name, value in fields if not value]
        if missing:
            raise ProviderError(
                f"`failover` routing needs each providerDef to declare {', '.join(missing)}"
            )
        routes[provider_id] = {
            "openai": False,
            "upstream": base_url,
            "api_key": base_env.get(api_key_env, ""),
            "models": {},
            "keys_by_wire": {},
        }
    # openai is a mapped primary (R1 PASS, notes/05): verbatim OAuth forwarding — the
    # bearer and ChatGPT-Account-Id arrive from the client's own wrapper.
    routes["openai"] = {
        "openai": True,
        "upstream": OPENAI_CODEX_UPSTREAM,
        "api_key": "",
        "models": {},
        "keys_by_wire": {},
    }
    for provider_id in routes:
        try:
            failover_route_base(provider_id)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc

    # Model catalogue per route: declared entries first (options + id overrides),
    # then agent model references as seeds for undeclared keys.
    oc_cfg = block_cfg.get("opencodeConfig") or {}
    declared_providers = oc_cfg.get("provider") or {}
    if not isinstance(declared_providers, dict):
        declared_providers = {}
    for provider_id, route in routes.items():
        models_cfg = (declared_providers.get(provider_id) or {}).get("models") or {}
        if isinstance(models_cfg, dict):
            for key, entry in models_cfg.items():
                if not isinstance(entry, dict):
                    continue
                route["models"][key] = {
                    "id": str(entry.get("id") or key),
                    "options": entry.get("options") or {},
                }
    agents_cfg = oc_cfg.get("agent") or {}
    if isinstance(agents_cfg, dict):
        for agent in agents_cfg.values():
            if not isinstance(agent, dict):
                continue
            model = str(agent.get("model") or "")
            provider_id, _, key = model.partition("/")
            route = routes.get(provider_id)
            if route is not None and key and key not in route["models"]:
                route["models"][key] = {"id": key, "options": {}}
    for route in routes.values():
        keys_by_wire: dict[str, list[str]] = {}
        for key, entry in route["models"].items():
            keys_by_wire.setdefault(entry["id"], []).append(key)
        route["keys_by_wire"] = keys_by_wire

    def _resolve_key(base: str) -> None:
        """A chain key / rung base resolves against the route table, or it's an error."""
        provider_id, _, key = base.partition("/")
        route = routes.get(provider_id)
        if route is None:
            raise ProviderError(
                f"failover key {base!r}: unknown provider {provider_id!r} — not a"
                " providerDef of this launcher"
            )
        if key not in route["models"]:
            raise ProviderError(
                f"failover key {base!r}: model key {key!r} is not declared for provider"
                f" {provider_id!r}"
            )

    rung_options_raw = block.get("rungOptions", {})
    if not isinstance(rung_options_raw, dict):
        raise ProviderError("`failover.rungOptions` must be a JSON object")
    rung_options: dict[str, dict] = {}
    for rung, options in rung_options_raw.items():
        if not isinstance(rung, str) or not rung:
            raise ProviderError(
                "`failover.rungOptions` keys must be non-empty runtime rung references"
            )
        if not isinstance(options, dict):
            raise ProviderError(f"failover rungOptions entry {rung!r} must be a JSON object")
        _resolve_key(_chain_base(rung))
        rung_options[rung] = options

    detect = block.get("detect")
    if not isinstance(detect, dict):
        raise ProviderError("`failover.detect` must be a JSON object")
    status = detect.get("status")
    if (
        not isinstance(status, list)
        or not status
        or not all(isinstance(code, int) and not isinstance(code, bool) for code in status)
    ):
        raise ProviderError(
            "`failover.detect.status` must be a non-empty list of HTTP status codes"
        )
    messages = detect.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(entry, str) and entry for entry in messages)
    ):
        raise ProviderError("`failover.detect.messages` must be a non-empty list of substrings")
    max_walk = block.get("maxWalk", 3)
    if not isinstance(max_walk, int) or isinstance(max_walk, bool) or max_walk < 1:
        raise ProviderError("`failover.maxWalk` must be a positive integer")
    vision = block.get("vision")
    if not isinstance(vision, dict) or not all(isinstance(flag, bool) for flag in vision.values()):
        raise ProviderError("`failover.vision` must be a JSON object of model key -> boolean")

    chains_raw = block.get("chains")
    if not isinstance(chains_raw, dict) or not chains_raw:
        raise ProviderError("`failover.chains` must be a non-empty JSON object")
    warnings: list[str] = []
    chains: dict[str, tuple[str, ...]] = {}
    for key, rungs in chains_raw.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(rungs, list)
            or not rungs
            or not all(isinstance(rung, str) and rung for rung in rungs)
        ):
            raise ProviderError(f"failover chain {key!r} must be a non-empty list of rung keys")
        base_key = _chain_base(key)
        _resolve_key(base_key)
        if base_key not in vision:
            raise ProviderError(
                f"failover chain key {base_key!r} has no `failover.vision` entry — a missing"
                " flag is a launch error, not a silent default"
            )
        pruned: list[str] = []
        seen: set[str] = set()
        for rung in rungs:
            rung_base = _chain_base(rung)
            if rung_base == base_key:
                raise ProviderError(f"failover chain {key!r} contains its own key as a rung")
            if rung_base.partition("/")[0] == "openai":
                warnings.append(
                    f"failover chain {key!r}: openai rung {rung!r} pruned — openai is not a"
                    " fallback target in v1 (the OAuth bearer only arrives on openai primaries)"
                )
                continue
            _resolve_key(rung_base)
            if rung_base not in vision:
                raise ProviderError(
                    f"failover rung {rung_base!r} (chain {key!r}) has no `failover.vision` entry"
                )
            if rung in seen:
                continue
            seen.add(rung)
            pruned.append(rung)
        if not pruned:
            raise ProviderError(f"failover chain {key!r} has no eligible rungs after pruning")
        chains[key] = tuple(pruned)

    unused_rung_options = sorted(
        set(rung_options) - {rung for chain in chains.values() for rung in chain}
    )
    if unused_rung_options:
        raise ProviderError(
            "failover.rungOptions contains unused runtime rung reference(s): "
            + ", ".join(repr(rung) for rung in unused_rung_options)
        )

    return (
        {
            "status": sorted(status),
            "messages": list(messages),
            "max_walk": max_walk,
            "vision": dict(vision),
            "chains": chains,
            "rung_options": rung_options,
            "routes": routes,
        },
        warnings,
    )


def _chain_base(key: str) -> str:
    """A chain key / rung without its ``@variant`` suffix (vision lookups strip it)."""
    return key.split("@", 1)[0]


def _apply_failover_routes(document: dict, failover: FailoverPlan) -> dict:
    """Point every routed provider's ``options.baseURL`` at the failover proxy.

    The resolved ``apiKey`` value stays — the proxy receives it but rewrites
    ``Authorization`` per rung, so the child never needs its real key to reach a
    fallback. The built-in ``openai`` provider is overlaid (entry created when absent)
    without touching its ``npm`` or the OAuth plugin — the R1-verified options-only
    insertion.
    """
    providers = dict(document.get("provider") or {})
    for provider_id in failover.routes:
        entry = dict(providers.get(provider_id) or {})
        options = dict(entry.get("options") or {})
        options["baseURL"] = f"{failover.base_url}{failover_route_base(provider_id)}"
        entry["options"] = options
        providers[provider_id] = entry
    merged = dict(document)
    merged["provider"] = providers
    return merged


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
    references like ``claude/deepseek`` or ``base/claude.json`` work. A ref with no
    recognised extension tries ``.json``, then ``.yaml``, then ``.yml``; an explicit ``.json``
    that does not exist falls back to its ``.yaml`` sibling — so a base converted to YAML
    keeps its old ``.json`` referrers working. An explicit ``.yaml`` / ``.yml`` resolves
    as-is. The same rule resolves both the ``agedum <value>`` argument and an ``extends``
    reference; a ref that resolves to no file surfaces as an error at load time.
    """
    candidate = Path(value) if value.startswith("/") else (base_dir or providers_dir()) / value
    suffix = candidate.suffix
    if suffix != JSON_SUFFIX and suffix not in YAML_SUFFIXES:
        # No recognised extension: try the conventional spellings in order (.json wins
        # when several exist, matching --providers), else keep the .json name so the
        # load error names the conventional file.
        for ext in (JSON_SUFFIX, ".yaml", ".yml"):
            with_ext = candidate.parent / f"{candidate.name}{ext}"
            if with_ext.is_file():
                return with_ext
        return candidate.parent / f"{candidate.name}{JSON_SUFFIX}"
    if suffix == JSON_SUFFIX and not candidate.is_file():
        yaml_sibling = candidate.with_suffix(".yaml")
        if yaml_sibling.is_file():
            return yaml_sibling
    return candidate


def config_format(path: Path) -> str:
    """The source format of a config file: ``"yaml"`` or ``"json"``.

    The format is the dispatch key of :func:`load_config_with_format` — a function of the
    suffix alone (``.yaml`` / ``.yml`` → YAML, anything else → JSON).
    """
    return "yaml" if path.suffix in YAML_SUFFIXES else "json"


class LoadedConfig(NamedTuple):
    """A parsed provider config plus its source format and declared schema.

    ``format`` lets ``--dry-run`` report the source without re-reading the file.
    ``schema`` is the **entry document's** declared envelope version
    (``agedum-provider/v1`` or ``/v2``) — the value that gates intent expansion.
    JSON documents carry no schema key and are v1 semantics forever, so they
    report ``agedum-provider/v1``.
    """

    config: dict
    format: str
    schema: str = PROVIDER_SCHEMA_VERSION


def load_config(path: Path) -> dict:
    """Read and parse a single provider config file (JSON, or YAML with the schema key).

    This is the raw, one-file load — it does **not** resolve ``include`` / ``extends``. Use
    :func:`load_merged_config` to get a config's effective (composition- and
    inheritance-resolved) form. Raises :class:`ProviderError`.
    """
    return load_config_with_format(path).config


def load_config_with_format(path: Path) -> LoadedConfig:
    """Like :func:`load_config`, returning the source format and declared schema.

    A ``.yaml`` / ``.yml`` file is parsed with ``yaml.safe_load``, must declare
    ``schema: agedum-provider/v1`` or ``agedum-provider/v2``
    (:class:`ProviderSchemaError` otherwise), and then yields the same dict the
    equivalent JSON document would: the ``schema`` key is stripped and every
    YAML 1.1 boolean trap in a string-valued slot is rejected
    (:class:`YamlBooleanTrapError`). Any other suffix parses as JSON and carries
    v1 semantics forever (v2 is YAML-only). The declared schema rides
    :class:`LoadedConfig` and gates intent expansion at the launch seam.
    """
    fmt = config_format(path)
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ProviderError(f"cannot read provider config {path}: {exc}") from exc
    if fmt == "yaml":
        config, schema = _load_yaml_document(raw, path)
    else:
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(config, dict):
            raise ProviderError(
                f"provider config {path} must be a JSON object, not {type(config).__name__}"
            )
        schema = PROVIDER_SCHEMA_VERSION
    return LoadedConfig(config, fmt, schema)


def _load_yaml_document(raw: str, path: Path) -> tuple[dict, str]:
    """Parse one YAML provider config into ``(envelope dict, declared schema)``.

    Both declared versions load here — per-file, so any document in an
    ``include`` / ``extends`` chain may declare either; which document's schema
    *gates expansion* is decided at the root (see :func:`expand_carrier_refs`).
    An unsupported version is the same :class:`ProviderSchemaError` the v1-only
    engines raise, naming the expected value.
    """
    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ProviderError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ProviderError(
            f"provider config {path} must be a YAML mapping, not {type(config).__name__}"
        )
    schema = config.get(PROVIDER_SCHEMA_KEY)
    if schema not in PROVIDER_SCHEMA_VERSIONS:
        raise ProviderSchemaError(
            f"{path}: YAML provider config must declare `{PROVIDER_SCHEMA_KEY}: "
            f"{PROVIDER_SCHEMA_VERSION}` (found {schema!r})"
        )
    _reject_yaml_boolean_traps(config, path)
    # Normalize to the envelope: the JSON form of the same document carries no version
    # key, so YAML must not land one in the merged config either (parse, not translate).
    config.pop(PROVIDER_SCHEMA_KEY, None)
    return config, schema


# The ``config``-block keys every harness consumes as a plain string — model names,
# endpoint URLs, auth styles, enum values (cline's ``compaction: off``, reasonix's
# ``autoPlan: on``). Deliberately absent: the keys a harness consumes as a boolean
# (``abstract``, claude's ``foldSystemMessages``, kimi's ``thinking``/``plan``/``yolo``,
# opencode's ``disableExternalSkills``) and the verbatim passthroughs (claude
# ``settings``, opencode ``opencodeConfig``, pi ``piSettings``/``piExtensionConfig``,
# codex ``codexConfig``) where a boolean is a legitimate value.
_STRING_CONFIG_KEYS = frozenset(
    {
        "model",
        "subagentModel",
        "baseUrl",
        "authStyle",
        "effortLevel",
        "upstreamApi",
        "openaiThinking",
        "smallFastModel",
        "haikuAlias",
        "sonnetAlias",
        "opusAlias",
        "binary",
        "providerType",
        "defaultEffort",
        "subagentEffort",
        "provider",
        "compaction",
        "kind",
        "plannerModel",
        "autoPlan",
        "weakModel",
        "editorModel",
        "reasoningEffort",
        "api",
        "wireApi",
        "codexAgents",
        "codexProjectAgents",
    }
)


def _reject_yaml_boolean_traps(config: dict, path: Path) -> None:
    """Reject unquoted YAML 1.1 booleans in the envelope's string-valued slots.

    pyyaml turns an unquoted ``on`` / ``off`` / ``yes`` / ``no`` into a Python boolean,
    so ``secretEnv: on`` or an ``extraEnv`` value of ``no`` would reach a harness as
    ``"True"`` / ``"False"`` (or fail confusingly) instead of the word the author wrote.
    Only the string-valued slots are walked — legitimate booleans are untouched — and
    only for YAML: JSON has no implicit booleans, so a JSON ``true`` is always authorial.
    Map-key collisions (``models: {on: …}`` also parses the key as a boolean) are not
    rejected here; they surface as the consumers' own fail-loud lookup errors.
    """

    def trap(key_path: str) -> None:
        raise YamlBooleanTrapError(
            f"{path}: yaml boolean trap at {key_path}: unquoted on/off/yes/no parsed as "
            "boolean — quote the value"
        )

    def check_string(value: object, key_path: str) -> None:
        if isinstance(value, bool):
            trap(key_path)

    def check_string_list(value: object, key_path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                check_string(item, f"{key_path}[{index}]")

    def check_string_map(value: object, key_path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                check_string(item, f"{key_path}.{key}")

    def check_ref(value: object, key_path: str) -> None:
        # A string-or-list-of-strings slot (``extends``, pi ``requireExtensions``).
        check_string(value, key_path)
        check_string_list(value, key_path)

    def check_provider_defs(defs: object, key_path: str) -> None:
        if isinstance(defs, dict):
            defs = [defs]
        if not isinstance(defs, list):
            return
        for index, entry in enumerate(defs):
            if not isinstance(entry, dict):
                continue
            base = f"{key_path}[{index}]"
            for key in ("id", "name", "npm", "kind", "baseUrl", "model", "apiKeyEnv", "api"):
                check_string(entry.get(key), f"{base}.{key}")

    def check_config_block(block: dict) -> None:
        for key in _STRING_CONFIG_KEYS:
            if key in block:
                check_string(block[key], f"config.{key}")
        # Env values are the classic trap (`extraEnv: {FOO: no}`), as are MCP entries.
        check_string_map(block.get("extraEnv"), "config.extraEnv")
        servers = block.get("mcpServers")
        if isinstance(servers, dict):
            for name, entry in servers.items():
                if not isinstance(entry, dict):
                    continue
                base = f"config.mcpServers.{name}"
                for key in ("command", "url", "cwd", "transport"):
                    check_string(entry.get(key), f"{base}.{key}")
                check_string_list(entry.get("args"), f"{base}.args")
                check_string_map(entry.get("env"), f"{base}.env")
                check_string_map(entry.get("headers"), f"{base}.headers")
        check_provider_defs(block.get("providerDef"), "config.providerDef")
        # opencode's option knobs flow through _clean_options, which silently drops any
        # non-string value — both the launcher-wide defaultOptions and the per-agent rows
        # (whose `agent` / `model` are likewise silently dropped or stringified).
        default_options = block.get("defaultOptions")
        if isinstance(default_options, dict):
            for key in ("reasoningEffort", "textVerbosity", "reasoningSummary"):
                check_string(default_options.get(key), f"config.defaultOptions.{key}")
        agent_rows = block.get("agentOptions")
        if isinstance(agent_rows, list):
            for index, row in enumerate(agent_rows):
                if not isinstance(row, dict):
                    continue
                base = f"config.agentOptions[{index}]"
                check_string(row.get("agent"), f"{base}.agent")
                check_string(row.get("model"), f"{base}.model")
                for key in ("reasoningEffort", "textVerbosity", "reasoningSummary"):
                    check_string(row.get(key), f"{base}.{key}")
        # kimi's `models` map (entries with per-model string knobs) vs pi's list of ids.
        declared = block.get("models")
        if isinstance(declared, list):
            check_string_list(declared, "config.models")
        elif isinstance(declared, dict):
            for model_id, entry in declared.items():
                if not isinstance(entry, dict):
                    continue
                base = f"config.models.{model_id}"
                check_string(entry.get("defaultEffort"), f"{base}.defaultEffort")
                check_string_list(entry.get("capabilities"), f"{base}.capabilities")
                check_string_list(entry.get("supportEfforts"), f"{base}.supportEfforts")
        # kimi's single-model form carries the same knobs flat on the config block.
        check_string_list(block.get("capabilities"), "config.capabilities")
        check_string_list(block.get("supportEfforts"), "config.supportEfforts")
        check_ref(block.get("requireExtensions"), "config.requireExtensions")
        check_string_list(block.get("modelInputs"), "config.modelInputs")
        catalog = block.get("codexModelCatalog")
        if isinstance(catalog, dict):
            check_string(catalog.get("displayName"), "config.codexModelCatalog.displayName")
            check_string(catalog.get("description"), "config.codexModelCatalog.description")

    for key in ("harness", "secretEnv", "slug"):
        check_string(config.get(key), key)
    check_ref(config.get("extends"), "extends")
    check_ref(config.get("include"), "include")
    check_ref(config.get("modelsCatalog"), "modelsCatalog")
    check_string_list(config.get("requiredEnv"), "requiredEnv")
    sandbox = config.get("sandbox")
    if isinstance(sandbox, dict):
        check_string_list(sandbox.get("readWrite"), "sandbox.readWrite")
    block = config.get("config")
    if isinstance(block, dict):
        check_config_block(block)


# File-level meta keys: consumed during resolution, never passed to the launch.
_META_KEYS = ("extends", "include", "abstract")


def _without_meta(config: dict) -> dict:
    """A copy of ``config`` without the meta keys (``extends`` / ``include`` / ``abstract``).

    ``abstract`` is a property of the file as authored, not of the merged result, so it is
    dropped here — a config extending or including an abstract fragment never inherits its
    abstractness."""
    return {key: value for key, value in config.items() if key not in _META_KEYS}


def _ref_list(config: dict, key: str) -> list[str]:
    """Normalise a config's string-or-list-of-strings ref key (``extends`` / ``include``)."""
    raw = config.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise ProviderError(f"`{key}` must be a string or a list of strings")


def _extends_refs(config: dict) -> list[str]:
    """Normalise a config's ``extends`` (string, list, or absent) to a list of refs."""
    return _ref_list(config, "extends")


def _include_refs(config: dict) -> list[str]:
    """Normalise a config's ``include`` (string, list, or absent) to a list of refs."""
    return _ref_list(config, "include")


def load_merged_config(
    path: Path, base_dir: Path | None = None, _seen: frozenset[Path] | None = None
) -> dict:
    """Load a provider config and resolve its ``include`` fragments + ``extends`` chain.

    Each ``include`` / ``extends`` reference resolves by the providers-root rule (see
    :func:`resolve_config_path`). Merge order for one file, most-default first: every
    ``include`` target (recursively resolved — a fragment may include and extend others),
    deep-merged left→right (earlier include = more default); then the ``extends`` chain,
    bases deep-merged left→right (a base's keys beat an included fragment's on conflict —
    inheritance overrides composition); then the extending config's own keys last (child
    wins). The meta keys (``extends`` / ``include`` / ``abstract``) are stripped from the
    result, and ``abstract`` is never inherited through either mechanism. A cycle in the
    combined include+extends graph raises :class:`ProviderError` (a file reached twice
    through different paths is fine — a DAG merge, not a tree walk).

    ``requiredEnv`` is the one key that **unions** rather than being overwritten — across
    includes, the extends chain, and the file's own keys alike; see :func:`_merge_extends`.
    """
    return load_merged_config_with_format(path, base_dir, _seen).config


def load_merged_config_with_format(
    path: Path, base_dir: Path | None = None, _seen: frozenset[Path] | None = None
) -> LoadedConfig:
    """Like :func:`load_merged_config`, also returning the entry file's source format.

    The reported format is the launched file's own, not its bases' or fragments' — a JSON
    config extending a YAML base still reports ``json``.
    """
    providers = base_dir or providers_dir()
    resolved = path.resolve()
    seen = _seen or frozenset()
    if resolved in seen:
        raise ProviderError(f"circular extends/include involving {path}")
    seen = seen | {resolved}
    raw = load_config_with_format(path)
    merged: dict = {}
    # (a) Includes — composition, the most-default layer: each target's *effective* config
    # (resolved recursively, its own includes and extends already applied) pasted in list
    # order, earlier include the more default.
    for ref in _include_refs(raw.config):
        fragment = load_merged_config_with_format(
            resolve_config_path(ref, providers), providers, seen
        )
        merged = _merge_extends(merged, fragment.config)
    # (b) The extends chain — inheritance overrides composition, so a base's keys beat an
    # included fragment's on conflict.
    for ref in _extends_refs(raw.config):
        base = load_merged_config_with_format(resolve_config_path(ref, providers), providers, seen)
        merged = _merge_extends(merged, base.config)
    # (c) The file's own keys last — the most specific layer. The **entry file's**
    # declared schema rides out: it is the root that gates intent expansion, whatever
    # the chain's bases and fragments declare.
    return LoadedConfig(_merge_extends(merged, _without_meta(raw.config)), raw.format, raw.schema)


def _merge_extends(base: dict, overlay: dict) -> dict:
    """Deep-merge one resolution step, unioning ``requiredEnv`` instead of replacing it.

    Serves both mechanisms: an ``extends`` chain step and an ``include`` composition step
    (composition needs exactly this — a deep merge whose one list exception is the env
    union). A plain deep-merge replaces lists wholesale, which for ``requiredEnv`` silently
    *drops* a base's requirement the moment the child declares one of its own — the child
    would launch with the base's token unvalidated and unexported, and whatever the base
    configured with it (an MCP server's ``${VAR}``, a provider key) would fail at first use
    rather than at launch. Requirements accumulate down an ``extends`` chain and across
    includes, so they are unioned; earlier order first, later additions appended,
    duplicates dropped.
    """
    merged = _deep_merge(base, overlay)
    required = [
        *(value for value in base.get("requiredEnv") or []),
        *(value for value in overlay.get("requiredEnv") or []),
    ]
    if required:
        merged["requiredEnv"] = list(dict.fromkeys(required))
    return merged


# ---------------------------------------------------------------------------
# the run-time model catalogue (`models.yaml`) + `modelRef` expansion
# ---------------------------------------------------------------------------


class ModelCatalog(NamedTuple):
    """A loaded model catalogue: the verbatim per-model fragments plus the
    optional ``carrierMeta`` facts section (an empty mapping when absent).

    ``models`` is what ``modelRef`` files verbatim; ``carrier_meta`` is what
    v2 intent expansion derives from — provider/family/efforts/display facts,
    plus Kimi's ``aliases``/``alias_model_id``.
    """

    models: dict[str, dict]
    carrier_meta: dict[str, dict]


def load_model_catalog(path: Path) -> dict[str, dict]:
    """Read and validate a model catalogue, returning its ``models`` map.

    Thin view over :func:`load_model_catalog_with_carrier_meta` — the file is
    read and validated once, whole (including ``carrierMeta``, which this
    function does not return).
    """
    return load_model_catalog_with_carrier_meta(path).models


def load_model_catalog_with_carrier_meta(path: Path) -> ModelCatalog:
    """Read and validate a model catalogue: a YAML document declaring
    ``schema: agedum-models/v1`` whose ``models`` map holds one entry per model id,
    plus an optional ``carrierMeta`` section of per-model expansion facts.

    Each ``models`` entry is a **verbatim per-model fragment** in opencode's own
    catalog vocabulary (``name``, ``limit: {context, output, …}``, ``attachment``,
    ``modalities: {input, output}``, plus any other key opencode consumes —
    ``variants``, ``options``, …), so what lands in a launch config is byte-for-byte
    what the generated oc configs carry inline. Entries are type-checked minimally —
    a mapping per model, ``name`` a string, ``attachment`` a boolean, ``limit``
    values integers or null, ``modalities`` values lists of strings — with errors
    naming the model id and key; every other key passes through untouched. The
    catalogue deliberately gets **no** YAML boolean-trap walk in v1 (a documented
    limit: quote a value that reads as on/off/yes/no).

    ``carrierMeta`` is the v2 expansion facts section — a mapping of catalogue key
    → ``{provider, family, efforts, display}`` plus, for model-alias families,
    ``aliases`` (effort → alias id) and ``alias_model_id`` (required iff
    ``aliases``). It is validated whenever the catalogue loads — a declared-but-
    broken facts section must not be silent — but v1 filing never reads it, and a
    catalogue without the section behaves exactly as before (0.60 engines ignore
    it entirely). Unknown keys inside an entry are ignored (data-file philosophy:
    future facts land here without a schema bump). Raises
    :class:`ProviderError` (absent/unreadable/invalid file) or
    :class:`ModelCatalogSchemaError` (schema/shape/type violations); returns the
    :class:`ModelCatalog` pair.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError as exc:
        raise ProviderError(
            f"no model catalogue at {path} — a config references `modelRef`; create it"
            " (or point the config's `modelsCatalog` at one)"
        ) from exc
    except OSError as exc:
        raise ProviderError(f"cannot read model catalogue {path}: {exc}") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ProviderError(f"invalid YAML in model catalogue {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ModelCatalogSchemaError(
            f"model catalogue {path} must be a YAML mapping, not {type(document).__name__}"
        )
    schema = document.get(PROVIDER_SCHEMA_KEY)
    if schema != MODEL_CATALOGUE_SCHEMA_VERSION:
        raise ModelCatalogSchemaError(
            f"{path}: model catalogue must declare `schema: "
            f"{MODEL_CATALOGUE_SCHEMA_VERSION}` (found {schema!r})"
        )
    models = document.get("models")
    if not isinstance(models, dict):
        raise ModelCatalogSchemaError(
            f"{path}: model catalogue `models` must be a mapping of model id → entry"
        )
    for model_id, entry in models.items():
        if not isinstance(entry, dict):
            raise ModelCatalogSchemaError(
                f"{path}: catalogue entry for model {model_id!r} must be a mapping, "
                f"not {type(entry).__name__}"
            )
        _validate_catalog_entry(model_id, entry, path)
    raw_meta = document.get("carrierMeta")
    if raw_meta is None:
        carrier_meta: dict[str, dict] = {}
    else:
        if not isinstance(raw_meta, dict):
            raise ModelCatalogSchemaError(
                f"{path}: model catalogue `carrierMeta` must be a mapping of "
                f"model id → facts, not {type(raw_meta).__name__}"
            )
        for model_id, meta in raw_meta.items():
            _validate_carrier_meta_entry(path, model_id, meta)
        carrier_meta = raw_meta
    return ModelCatalog(models, carrier_meta)


def _validate_carrier_meta_entry(path: Path, model_id: str, meta: object) -> None:
    """Validate one ``carrierMeta`` entry; errors name the model and the key.

    Required facts: ``provider`` / ``family`` / ``display`` non-empty strings and
    ``efforts`` a non-empty list inside :data:`EFFORT_ALPHABET`. The model-alias
    pair is conditional: ``aliases`` (alphabet efforts → non-empty strings) may be
    present only with ``alias_model_id``, and ``alias_model_id`` only with
    ``aliases``. Anything else inside the entry passes through — future facts
    (phase 3 adds ``vision``) land without a catalogue change.
    """

    def bad(key: str, expectation: str, value: object) -> ModelCatalogSchemaError:
        return ModelCatalogSchemaError(
            f"{path}: carrierMeta entry {model_id!r} key `{key}` must be {expectation}, "
            f"got {value!r}"
        )

    if not isinstance(meta, dict):
        raise ModelCatalogSchemaError(
            f"{path}: carrierMeta entry for model {model_id!r} must be a mapping, "
            f"not {type(meta).__name__}"
        )
    for key in ("provider", "family", "display"):
        value = meta.get(key)
        if not isinstance(value, str) or not value.strip():
            raise bad(key, "a non-empty string", value)
    efforts = meta.get("efforts")
    if (
        not isinstance(efforts, list)
        or not efforts
        or not all(isinstance(effort, str) and effort in EFFORT_ALPHABET for effort in efforts)
    ):
        raise bad("efforts", f"a non-empty list of efforts in {EFFORT_ALPHABET}", efforts)
    aliases = meta.get("aliases")
    alias_model_id = meta.get("alias_model_id")
    if aliases is None and alias_model_id is None:
        return
    if aliases is not None:
        if not isinstance(aliases, dict) or not all(
            isinstance(effort, str)
            and effort in EFFORT_ALPHABET
            and isinstance(alias, str)
            and alias.strip()
            for effort, alias in aliases.items()
        ):
            raise bad(
                "aliases",
                f"a mapping of efforts in {EFFORT_ALPHABET} to non-empty strings",
                aliases,
            )
        if not isinstance(alias_model_id, str) or not alias_model_id.strip():
            raise bad(
                "alias_model_id",
                "a non-empty string (required when `aliases` is present)",
                alias_model_id,
            )
    else:
        raise bad("alias_model_id", "omitted when `aliases` is absent", alias_model_id)


def _validate_catalog_entry(model_id: str, entry: dict, path: Path) -> None:
    """Minimal type validation of one catalogue entry; errors name the model + key.

    Only the oc catalog vocabulary's own slots are checked — ``name`` (string),
    ``attachment`` (boolean), ``limit`` (integer-or-null values), ``modalities``
    (string-list values). Anything else passes verbatim: the catalogue is a fragment
    *source*, and opencode's entry vocabulary is opencode's to evolve.
    """

    def bad(key: str, expectation: str, value: object) -> ModelCatalogSchemaError:
        return ModelCatalogSchemaError(
            f"{path}: catalogue entry {model_id!r} key `{key}` must be {expectation}, got {value!r}"
        )

    name = entry.get("name")
    if name is not None and not isinstance(name, str):
        raise bad("name", "a string", name)
    attachment = entry.get("attachment")
    if attachment is not None and not isinstance(attachment, bool):
        raise bad("attachment", "a boolean", attachment)
    limit = entry.get("limit")
    if limit is not None:
        if not isinstance(limit, dict):
            raise bad("limit", "a mapping of integer-or-null values", limit)
        for key, value in limit.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise bad(f"limit.{key}", "an integer or null", value)
    modalities = entry.get("modalities")
    if modalities is not None:
        if not isinstance(modalities, dict):
            raise bad("modalities", "a mapping of string-list values", modalities)
        for key, value in modalities.items():
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise bad(f"modalities.{key}", "a list of strings", value)


def _model_ref_ids(provider_def: dict) -> list[str]:
    """A providerDef's ``modelRef`` as a list of catalogue ids (string or list form)."""
    raw = provider_def.get("modelRef")
    if raw is None:
        return []
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(item, str) and item.strip() for item in raw)
    ):
        return [item.strip() for item in raw]
    raise ProviderError("`modelRef` must be a model-id string or a non-empty list of them")


def expand_model_refs(config: dict, base_dir: Path | None = None) -> dict:
    """Expand ``modelRef`` keys in a *merged* config's opencode providerDefs against
    the model catalogue, returning the effective config.

    Opt-in expansion, applied after include/extends merging and before launch
    building (so ``--dry-run`` shows the effective result): a providerDef's
    ``modelRef`` (a catalogue id, or a list of them) files that catalogue entry —
    verbatim — under ``config.opencodeConfig.provider.<providerDef id>.models.<id>``,
    exactly where the generated oc configs carry their catalog inline; an authored
    inline entry for the same model is kept and wins on conflict. The ``modelRef``
    key itself is consumed and never reaches the launch.

    The catalogue lives at ``<providers root>/models.yaml`` unless the config's
    top-level ``modelsCatalog`` ref (resolved like ``include``, YAML-only — a
    non-``.yaml`` ref is a named error) points elsewhere; that key is a meta key,
    consumed here like ``extends``/``include``. Declaring ``modelsCatalog`` loads
    and validates the pointed-at catalogue even when nothing references it — a
    declared-but-broken pointer must not be silent.

    Zero-behaviour-change guarantee: a config with no ``modelRef`` anywhere and no
    ``modelsCatalog`` is returned untouched (the catalogue is never even read), so
    existing configs — and the generated oc JSON — are unaffected. Expansion is
    **opencode-first**: ``modelRef`` on any other harness fails loudly rather than
    being silently ignored (claude/codex need no expansion today).
    """
    catalog_ref = config.get("modelsCatalog")
    result = {key: value for key, value in config.items() if key != "modelsCatalog"}
    block = config.get("config")
    defs_raw = block.get("providerDef") if isinstance(block, dict) else None
    if isinstance(defs_raw, list):
        candidates = defs_raw
    elif isinstance(defs_raw, dict):
        candidates = [defs_raw]
    else:
        candidates = []
    has_refs = any(isinstance(entry, dict) and "modelRef" in entry for entry in candidates)
    if not has_refs and catalog_ref is None:
        return result

    providers = base_dir or providers_dir()
    if catalog_ref is not None:
        if not isinstance(catalog_ref, str) or not catalog_ref.strip():
            raise ProviderError("`modelsCatalog` must be a catalogue ref string")
        catalog_path = resolve_config_path(catalog_ref, providers)
        if catalog_path.suffix not in YAML_SUFFIXES:
            raise ProviderError(
                f"`modelsCatalog` {catalog_ref!r} must resolve to a .yaml catalogue "
                f"(got {catalog_path.name}); the catalogue is YAML-only in v1"
            )
    else:
        catalog_path = providers / MODEL_CATALOGUE_NAME
    catalog = load_model_catalog(catalog_path)
    if not has_refs:
        return result

    if config.get("harness") != "opencode":
        raise ProviderError(
            "`modelRef` is only implemented for the opencode harness — expansion "
            "writes an opencode catalog block (claude/codex need none today)"
        )
    # Functional update along the one mutated path (opencodeConfig.provider.<id>
    # .models.<ref>): build_launch must not see (or share) mutated sub-dicts.
    new_block = dict(block)
    passthrough = new_block.get("opencodeConfig")
    if passthrough is not None and not isinstance(passthrough, dict):
        raise ProviderError("opencodeConfig must be a JSON object")
    oc = dict(passthrough or {})
    oc_providers = dict(oc.get("provider") or {})
    for provider_def in _provider_defs(defs_raw):
        refs = _model_ref_ids(provider_def)
        if not refs:
            continue
        provider_id = str(provider_def.get("id") or "").strip()
        if not provider_id:
            raise ProviderError(
                "a providerDef carrying `modelRef` needs an `id` — the catalogue "
                "fragments are filed under opencodeConfig.provider.<id>.models"
            )
        for ref in refs:
            if ref not in catalog:
                raise ProviderError(
                    f"modelRef {ref!r} (providerDef {provider_id!r}) is not in the "
                    f"model catalogue {catalog_path}"
                )
        entry = dict(oc_providers.get(provider_id) or {})
        models = dict(entry.get("models") or {})
        for ref in refs:
            inline = models.get(ref)
            models[ref] = _deep_merge(
                catalog[ref], dict(inline) if isinstance(inline, dict) else {}
            )
        entry["models"] = models
        oc_providers[provider_id] = entry
    oc["provider"] = oc_providers
    new_block["opencodeConfig"] = oc

    def stripped(provider_def: dict) -> dict:
        return {key: value for key, value in provider_def.items() if key != "modelRef"}

    if isinstance(defs_raw, dict):
        new_block["providerDef"] = stripped(defs_raw)
    else:
        new_block["providerDef"] = [
            stripped(provider_def) for provider_def in _provider_defs(defs_raw)
        ]
    result["config"] = new_block
    return result


# ---------------------------------------------------------------------------
# `agedum-provider/v2` — the effort-carrier grammar at run time
# ---------------------------------------------------------------------------

# The effort alphabet and its canonical order (the builder's VALID_EFFORTS and
# declared_efforts): refs are `<catalogue key>@<effort>`, declared efforts are
# enumerated high-first regardless of authoring order, and every catalogue
# `carrierMeta.efforts` / `aliases` entry must be inside it.
EFFORT_ALPHABET = ("high", "low")

# Family → effort carrier (the builder's EFFORT_CARRIERS): where a declared
# effort lands in the derived output. ``variant`` → a `variant` field on the
# agent entry (gpt); ``reasoningEffort`` → `options.reasoningEffort` on the
# agent entry (deepseek/glm); ``model-alias`` → the effort-suffixed alias on
# the agent's `model`, with the effort carried by the filed alias entries'
# `options.thinking.effort` (kimi agents carry neither `variant` nor
# `options`). Grammar, not policy: a new family here is an engine release.
EFFORT_CARRIERS = {
    "gpt": "variant",
    "deepseek": "reasoningEffort",
    "glm": "reasoningEffort",
    "kimi": "model-alias",
}

# OpenCode 1.18.23's complete known GPT variant vocabulary — compatibility
# data for derived GPT catalogues (every variant the config does not declare
# is filed disabled, in this order), not a claim about variants a future
# OpenCode may add.
OPENCODE_1_18_23_GPT_VARIANTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def _intent_markers(config: dict) -> list[str]:
    """Describe the expansion-intent markers a config carries, in scan order.

    Intent is `@`-refs on an opencode config's model slots (`config.model` and
    the `opencodeConfig.agent` entries' `model` fields) plus the top-level
    `expansionModels` universe key. Scoped deliberately: an `@` inside a prompt,
    a description, or a non-opencode `model` value is not intent.
    """
    markers: list[str] = []
    if "expansionModels" in config:
        markers.append("top-level `expansionModels`")
    block = config.get("config")
    if config.get("harness") == "opencode" and isinstance(block, dict):
        model = block.get("model")
        if isinstance(model, str) and "@" in model:
            markers.append("`config.model` `@`-ref")
        oc = block.get("opencodeConfig")
        agents = oc.get("agent") if isinstance(oc, dict) else None
        if isinstance(agents, dict):
            for name, entry in agents.items():
                agent_model = entry.get("model") if isinstance(entry, dict) else None
                if isinstance(agent_model, str) and "@" in agent_model:
                    markers.append(f"agent {name!r} `model` `@`-ref")
    return markers


def expand_carrier_refs(
    config: dict,
    root_schema: str,
    base_dir: Path | None = None,
    catalog_ref: str | None = None,
) -> dict:
    """Apply `agedum-provider/v2` intent expansion to a *merged* config.

    Runs at the launch seam, after :func:`expand_model_refs` (modelRef filing
    first, intent expansion second, so derived entries merge under what is
    already filed). The **root document's** declared schema gates it — a v2 base
    under a v1 root does not expand, and a v1 base under a v2 root does.

    A v1 root (including every JSON document — v1 semantics forever) carrying
    intent markers gets a named :class:`ProviderSchemaError` telling the author
    to declare v2: today such a file would launch with a garbage model ref that
    fails far from the cause.

    A v2 root expands (see the ``_expand_v2`` section below); with **no intent
    markers at all** expansion is a no-op that only strips the (absent or empty)
    `expansionModels` key — declaring v2 alone is not intent, which is what keeps
    a marker-free v2 document legal on a non-opencode harness.
    ``catalog_ref`` is the merged config's `modelsCatalog` pointer read before
    :func:`expand_model_refs` consumed it; ``base_dir`` anchors refs like
    everywhere else.
    """
    if root_schema != PROVIDER_SCHEMA_VERSION_2:
        markers = _intent_markers(config)
        if markers:
            raise ProviderSchemaError(
                "this config looks like expansion intent ("
                + "; ".join(markers)
                + f") but declares `{PROVIDER_SCHEMA_VERSION}` — declare "
                f"`{PROVIDER_SCHEMA_KEY}: {PROVIDER_SCHEMA_VERSION_2}` to license expansion"
            )
        return config
    return _expand_v2(config, base_dir, catalog_ref)


class _ModelRef(NamedTuple):
    """One resolved `@`-ref: the catalogue key, the effort, and the carrier."""

    key: str
    effort: str
    carrier: str
    meta: dict


def _parse_carrier_ref(text: str, where: str) -> tuple[str, str]:
    """Split one ``<catalogue key>@<effort>`` ref into ``(key, effort)``."""
    key, sep, effort = text.partition("@")
    if not sep or not key.strip() or not effort.strip():
        raise ExpansionError(f"{where}: {text!r} is not a `<catalogue key>@<effort>` ref")
    return key, effort


def _collect_universe(config: dict) -> list[tuple[str, str, str]]:
    """The config's expansion universe as ``(key, effort, where)`` refs, in
    first-appearance order: `config.model`, then the `opencodeConfig.agent`
    entries in mapping order, then `expansionModels`. This walk order is the
    provider filing order (Decision 4) — no engine constant participates.
    """
    universe: list[tuple[str, str, str]] = []
    block = config.get("config")
    if isinstance(block, dict):
        model = block.get("model")
        if isinstance(model, str) and "@" in model:
            key, effort = _parse_carrier_ref(model, "`config.model`")
            universe.append((key, effort, "`config.model`"))
        oc = block.get("opencodeConfig")
        agents = oc.get("agent") if isinstance(oc, dict) else None
        if isinstance(agents, dict):
            for name, entry in agents.items():
                if not isinstance(entry, dict):
                    continue
                agent_model = entry.get("model")
                if isinstance(agent_model, str) and "@" in agent_model:
                    where = f"agent {name!r}"
                    key, effort = _parse_carrier_ref(agent_model, where)
                    universe.append((key, effort, where))
    expansion = config.get("expansionModels")
    if expansion is not None:
        if not isinstance(expansion, list) or not all(
            isinstance(item, str) and item.strip() for item in expansion
        ):
            raise ExpansionError(
                "`expansionModels` must be a list of `<catalogue key>@<effort>` refs"
            )
        for item in expansion:
            key, effort = _parse_carrier_ref(item, "`expansionModels`")
            universe.append((key, effort, "`expansionModels`"))
    return universe


def _resolve_carrier_ref(
    key: str, effort: str, where: str, catalog: dict, carrier_meta: dict
) -> _ModelRef:
    """Resolve one ref against the catalogue + carrierMeta, failing loudly.

    Error inventory (Decision 4): unknown catalogue key; effort outside the
    alphabet; effort outside the model's declared efforts; a referenced model
    with no `carrierMeta` entry; a family with no effort carrier; a model-alias
    model without `aliases`/`alias_model_id`; a ref effort missing from
    `aliases`.
    """
    if effort not in EFFORT_ALPHABET:
        raise ExpansionError(
            f"{where}: effort {effort!r} is not in the effort alphabet {EFFORT_ALPHABET}"
        )
    if key not in catalog:
        raise ExpansionError(f"{where}: catalogue key {key!r} is not in the model catalogue")
    meta = carrier_meta.get(key)
    if meta is None:
        raise ExpansionError(
            f"{where}: model {key!r} has no `carrierMeta` entry in the catalogue — "
            "expansion needs its provider/family/efforts facts"
        )
    efforts = meta.get("efforts")
    if not isinstance(efforts, list) or effort not in efforts:
        raise ExpansionError(
            f"{where}: effort {effort!r} is not one of model {key!r}'s declared "
            f"efforts ({', '.join(map(str, efforts or []))})"
        )
    family = meta.get("family")
    carrier = EFFORT_CARRIERS.get(family)
    if carrier is None:
        raise ExpansionError(
            f"{where}: model {key!r} family {family!r} has no effort carrier — "
            f"expansion covers {', '.join(sorted(EFFORT_CARRIERS))}"
        )
    if carrier == "model-alias":
        aliases = meta.get("aliases")
        alias_model_id = str(meta.get("alias_model_id") or "").strip()
        if not isinstance(aliases, dict) or not aliases or not alias_model_id:
            raise ExpansionError(
                f"{where}: model {key!r} (family {family!r}) is a model-alias carrier "
                "but its carrierMeta has no `aliases`/`alias_model_id`"
            )
        if effort not in aliases:
            raise ExpansionError(
                f"{where}: effort {effort!r} is not in model {key!r}'s `aliases` "
                f"({', '.join(sorted(aliases))})"
            )
    return _ModelRef(key, effort, carrier, meta)


def _ref_model_value(ref: _ModelRef) -> str:
    """The runtime model value for one ref: ``provider/key``, or for a
    model-alias carrier ``provider/<aliases[effort]>`` (``model_id`` is
    mechanically ``carrierMeta.provider + '/' + catalogue key``)."""
    if ref.carrier == "model-alias":
        return f"{ref.meta['provider']}/{ref.meta['aliases'][ref.effort]}"
    return f"{ref.meta['provider']}/{ref.key}"


def _translated_agent_entry(entry: dict, ref: _ModelRef, where: str) -> dict:
    """One agent entry with its `@`-ref translated per carrier.

    An entry that *authored* a carrier field (`variant` or
    `options.reasoningEffort`) while its model carries `@` is a conflict — the
    engine refuses to guess which effort wins. A `reasoningEffort` carrier
    merges into an existing authored `options` mapping (one without
    `reasoningEffort`, which the conflict rule guarantees); a `variant` carrier
    sets `variant`; a model-alias carrier sets nothing — the effort rides the
    alias.
    """
    authored = "variant" if "variant" in entry else None
    options = entry.get("options")
    if isinstance(options, dict) and "reasoningEffort" in options:
        authored = authored or "options.reasoningEffort"
    if authored:
        raise ExpansionError(
            f"{where}: the entry authors a carrier field (`{authored}`) but its model "
            f"is an `@`-ref ({entry.get('model')!r}) — write the intent form or the "
            "derived form, not both"
        )
    updated = dict(entry)
    updated["model"] = _ref_model_value(ref)
    if ref.carrier == "variant":
        updated["variant"] = ref.effort
    elif ref.carrier == "reasoningEffort":
        merged_options = dict(options) if isinstance(options, dict) else {}
        merged_options["reasoningEffort"] = ref.effort
        updated["options"] = merged_options
    return updated


def _derived_catalog_entries(
    key: str, meta: dict, carrier: str, declared_efforts: list[str], catalog: dict
) -> dict[str, dict]:
    """The catalog entries one model's universe files: ``filing key → entry``.

    deepseek/glm file the catalogue fragment verbatim under the catalogue key.
    gpt adds the variant disable-map — every OpenCode 1.18.23 variant the
    config does not declare, keyed in vocabulary order (config-scoped: the
    universe's declared efforts decide, and an author who wants a non-declared
    variant left enabled writes the catalog entry inline, which wins on
    merge). kimi files one alias-keyed entry per declared effort, canonical
    order, each carrying `options.thinking.{type: enabled, effort}`, with the
    low entry rebuilt as the `{id: alias_model_id, name: "<display> (low
    thinking)", …}` redirect — construction order mirrors the builder's
    `_catalog` for reviewability.
    """
    fragment = catalog[key]
    if carrier == "model-alias":
        entries: dict[str, dict] = {}
        for effort in declared_efforts:
            block = dict(fragment)
            block["options"] = {"thinking": {"type": "enabled", "effort": effort}}
            if effort == "low":
                block = {
                    "id": meta["alias_model_id"],
                    "name": f"{meta['display']} (low thinking)",
                    **{k: v for k, v in block.items() if k != "name"},
                }
            entries[meta["aliases"][effort]] = block
        return entries
    block = dict(fragment)
    if carrier == "variant":
        declared = set(declared_efforts)
        block["variants"] = {
            variant: {"disabled": True}
            for variant in OPENCODE_1_18_23_GPT_VARIANTS
            if variant not in declared
        }
    return {key: block}


def _expand_v2(config: dict, base_dir: Path | None = None, catalog_ref: str | None = None) -> dict:
    """Expand a merged v2 config's intent into the effective launch config.

    Pure dict transform, deterministic by construction: the universe walk fixes
    the provider filing order, canonical order fixes alias entry order, and
    nothing reads state beyond the catalogue file. Derived entries merge
    **under** what is already filed (modelRef output, authored inline entries),
    the same authored-wins rule `modelRef` applies. `failover` blocks pass
    through untouched (phase-3 scope). The bare-key error on `config.model`
    fires only when the catalogue is loaded — with zero intent markers the
    whole expansion is a no-op and a bare string is indistinguishable from a
    plain model id.
    """
    universe = _collect_universe(config)
    result = {key: value for key, value in config.items() if key != "expansionModels"}
    if not universe:
        return result
    if config.get("harness") != "opencode":
        raise ExpansionError(
            "expansion intent (`@`-refs / `expansionModels`) is only implemented for "
            "the opencode harness — expansion writes an opencode catalog block "
            "(other harnesses have no carrier fields to derive today)"
        )

    providers_root = base_dir or providers_dir()
    if catalog_ref is not None:
        if not isinstance(catalog_ref, str) or not catalog_ref.strip():
            raise ExpansionError("`modelsCatalog` must be a catalogue ref string")
        catalog_path = resolve_config_path(catalog_ref, providers_root)
        if catalog_path.suffix not in YAML_SUFFIXES:
            raise ExpansionError(
                f"`modelsCatalog` {catalog_ref!r} must resolve to a .yaml catalogue "
                f"(got {catalog_path.name}); the catalogue is YAML-only in v1"
            )
    else:
        catalog_path = providers_root / MODEL_CATALOGUE_NAME
    catalog_document = load_model_catalog_with_carrier_meta(catalog_path)
    catalog, carrier_meta = catalog_document.models, catalog_document.carrier_meta

    # Resolve the whole universe first-appearance, one plan per catalogue key:
    # carrier + facts + the set of declared efforts (canonical order at use).
    key_order: list[str] = []
    plans: dict[str, tuple[_ModelRef, set[str]]] = {}
    for key, effort, where in universe:
        ref = _resolve_carrier_ref(key, effort, where, catalog, carrier_meta)
        if key not in plans:
            key_order.append(key)
            plans[key] = (ref, set())
        plans[key][1].add(effort)

    # Functional update along the one mutated path (opencodeConfig), so
    # build_launch never sees (or shares) mutated sub-dicts — the modelRef
    # expansion's discipline.
    block = result.get("config")
    new_block = dict(block) if isinstance(block, dict) else {}
    passthrough = new_block.get("opencodeConfig")
    if passthrough is not None and not isinstance(passthrough, dict):
        raise ExpansionError("opencodeConfig must be a JSON object")
    oc = dict(passthrough or {})
    oc_providers = dict(oc.get("provider") or {})

    # (1) File derived catalog entries under carrierMeta.provider — providers in
    # first-appearance order of their models' refs; authored/filed entries win.
    for key in key_order:
        ref, efforts = plans[key]
        declared_efforts = [effort for effort in EFFORT_ALPHABET if effort in efforts]
        provider_id = ref.meta["provider"]
        entry = dict(oc_providers.get(provider_id) or {})
        models = dict(entry.get("models") or {})
        derived = _derived_catalog_entries(key, ref.meta, ref.carrier, declared_efforts, catalog)
        for filing_key, derived_entry in derived.items():
            inline = models.get(filing_key)
            models[filing_key] = _deep_merge(
                derived_entry, dict(inline) if isinstance(inline, dict) else {}
            )
        entry["models"] = models
        oc_providers[provider_id] = entry

    # (2) Translate the agent entries' `@`-refs in place (mapping order kept).
    agents = oc.get("agent")
    if isinstance(agents, dict):
        translated = dict(agents)
        for name, entry in agents.items():
            if not isinstance(entry, dict):
                continue
            model = entry.get("model")
            if not isinstance(model, str) or "@" not in model:
                continue
            where = f"agent {name!r}"
            key, effort = _parse_carrier_ref(model, where)
            ref = _resolve_carrier_ref(key, effort, where, catalog, carrier_meta)
            translated[name] = _translated_agent_entry(entry, ref, where)
        oc["agent"] = translated

    # (3) `config.model` is authoring sugar: `@`-refs translate by the same ref
    # function; plain `provider/model` strings pass through untouched; a bare
    # catalogue key (no `@`, no `/`) is the one authoring mistake with no plain
    # reading — named, per the design's Risk 4.
    model = new_block.get("model")
    if isinstance(model, str):
        if "@" in model:
            key, effort = _parse_carrier_ref(model, "`config.model`")
            ref = _resolve_carrier_ref(key, effort, "`config.model`", catalog, carrier_meta)
            new_block["model"] = _ref_model_value(ref)
        elif "/" not in model and model in catalog:
            raise ExpansionError(
                f"`config.model` {model!r} is a bare catalogue key — a bare key is not a "
                f"model ref in v2, only `key@effort` resolves; declare the effort "
                f"(`{model}@high` / `{model}@low`). Plain `provider/model` strings pass "
                "through untouched"
            )

    oc["provider"] = oc_providers
    new_block["opencodeConfig"] = oc
    result["config"] = new_block
    return result


@dataclass(frozen=True)
class ProviderSummary:
    """One row of ``agedum --providers``: a provider config reduced to its listing fields.

    ``name`` is the reference passed to ``agedum <name>`` — the config's path **relative to
    the providers root**, without its extension (e.g. ``claude/deepseek``). ``harness`` and
    ``model`` come from the config's *effective* (extends-resolved) form (``None`` when
    absent). ``error`` is set instead when the file could not be read, parsed, or resolved,
    so a single bad config never aborts the listing.
    """

    name: str
    path: Path
    harness: str | None = None
    model: str | None = None
    error: str | None = None


def _summary_model(block: dict) -> str:
    """The model to show for one config in the ``--providers`` roster.

    ``config.model`` is the usual home, but a native claude launcher has no endpoint for the
    model env to attach to and pins its default through ``config.settings`` instead — so fall
    back to that, or the roster reports the launcher as model-less.
    """
    model = str(block.get("model") or "").strip()
    if model:
        return model
    settings = block.get("settings")
    if isinstance(settings, dict):
        return str(settings.get("model") or "").strip()
    return ""


def list_providers(directory: Path | None = None) -> list[ProviderSummary]:
    """Summarise every launchable provider config under ``directory`` (default:
    :func:`providers_dir`), recursively, sorted by name.

    Walks subdirectories for ``*.json``, ``*.yaml`` and ``*.yml``; each config's ``name``
    is its path relative to the root, extension stripped. When both extensions exist for
    the same stem the ``.json`` file is the one listed (the name would collide otherwise).
    ``abstract: true`` configs (bases) are skipped. ``harness`` / ``model`` come from the
    effective (extends-resolved) config; an unreadable, invalid, or unresolvable config
    yields a summary with ``error`` set rather than raising. A missing directory yields an
    empty list.
    """
    target = directory or providers_dir()
    summaries: list[ProviderSummary] = []
    if not target.is_dir():
        return summaries
    # sorted() over the merged set keeps today's name ordering and puts the .json file
    # ahead of its .yaml sibling for the same stem ("x.json" < "x.yaml"), so the dedup
    # below makes .json win. The fixed model catalogue at the root is data, not a
    # config — without the skip it would surface as a broken roster row (it carries no
    # provider schema). Only that exact root-level filename is skipped: a `models.yaml`
    # in a subdirectory stays an ordinary config candidate (and errors loudly when
    # malformed).
    catalogue = target / MODEL_CATALOGUE_NAME
    all_paths = sorted(
        {
            path
            for pattern in ("*.json", "*.yaml", "*.yml")
            for path in target.rglob(pattern)
            if path != catalogue
        }
    )
    seen_names: set[str] = set()
    for path in all_paths:
        name = path.relative_to(target).with_suffix("").as_posix()
        if name in seen_names:
            continue
        seen_names.add(name)
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
        model = _summary_model(block) if isinstance(block, dict) else ""
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


def build_launch(
    config: dict,
    base_env: dict[str, str],
    *,
    label: str | None = None,
    failover: FailoverPlan | None = None,
) -> Launch:
    """Resolve a parsed provider ``config`` into a :class:`Launch` using ``base_env``
    (typically ``os.environ`` overlaid with the parsed ``.env``).

    ``config`` is the *effective* config (already extends-resolved). ``label`` is the
    provider's display name — the config path the user invoked; it falls back to
    :func:`provider_label` when not given. Validates the harness and that every required var
    is present and non-empty in ``base_env``; raises :class:`ProviderError` otherwise.
    ``failover`` (opencode only) rewrites the routed providers' ``options.baseURL`` to the
    running failover proxy; ``None`` — any config without a ``failover`` block — changes
    nothing (the rollback guarantee).
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
    builder = builders[harness]
    if harness == "opencode" and failover is not None:
        builder = functools.partial(builder, failover=failover)
    extra, unset, command, config_files = builder(block, secret_env, base_env)
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
    * **kimi** — Kimi Code's ``--prompt`` runs one prompt non-interactively and exits (it
      dropped the old ``--print`` flag), which is ``--run``. There is no seed-then-stay
      interactive mode, so ``--prompt`` (interactive) raises :class:`ProviderError`.
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
        # Kimi Code's --prompt runs one prompt non-interactively and exits (it dropped the old
        # --print flag) — that is --run. There is no "seed then stay interactive" mode, so
        # --prompt fails loudly (condash then falls back to spawn-and-type), like reasonix/aider.
        if interactive:
            raise ProviderError(
                "kimi has no interactive prompt-seeding (`--prompt` runs once and exits); "
                "use --run for a one-shot task, or launch without --prompt for an "
                "interactive session"
            )
        # --prompt is non-interactive and manages approvals itself: Kimi Code rejects combining
        # it with the interactive permission flags (`--prompt` + `--yolo`/`--auto` both error),
        # and `--plan` is interactive-only. Drop them from the seed command, keeping --model.
        seed_flags = [flag for flag in base_flags if flag not in ("--yolo", "--auto", "--plan")]
        return [binary, *seed_flags, *rest, "--prompt", text]
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
# canonical `mcpServers` -> each harness's own MCP dialect
# ---------------------------------------------------------------------------

# The placeholder an `mcpServers` value uses for something that must come from the
# environment at launch (an API token, a path). agedum **respells** it per harness and never
# resolves it: resolving would bake the secret into argv (claude's --mcp-config) or into an
# env var (opencode's config document), where --dry-run and the process list would show it.
# Left as a placeholder, the value is expanded by the harness itself, out of the environment
# `requiredEnv` populated from the agents .env.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _mcp_servers(block: dict) -> dict:
    """The config's canonical ``mcpServers`` map, validated; ``{}`` when absent.

    An entry is either **stdio** — ``command`` plus optional ``args`` / ``env`` / ``cwd`` —
    or **remote** — ``url`` plus optional ``headers`` / ``transport``. The two forms are
    mutually exclusive, and each harness's emitter below translates them into its own
    dialect. Values may carry ``${VAR}`` placeholders (see :data:`_ENV_PLACEHOLDER`).
    """
    servers = block.get("mcpServers")
    if not servers:
        return {}
    if not isinstance(servers, dict):
        raise ProviderError("`mcpServers` must be an object keyed by server name")
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise ProviderError(f"`mcpServers.{name}` must be an object")
        command = str(entry.get("command") or "").strip()
        url = str(entry.get("url") or "").strip()
        if command and url:
            raise ProviderError(
                f"`mcpServers.{name}` sets both `command` and `url`; an entry is stdio or remote"
            )
        if not command and not url:
            raise ProviderError(
                f"`mcpServers.{name}` needs a `command` (stdio) or a `url` (remote)"
            )
        args = entry.get("args")
        if args is not None and not isinstance(args, list):
            raise ProviderError(f"`mcpServers.{name}.args` must be a list")
    return servers


def _rewrite_env_placeholders(value: object, respell) -> object:
    """Recursively respell every ``${VAR}`` in ``value``'s strings via ``respell(var)``."""
    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(lambda match: respell(match.group(1)), value)
    if isinstance(value, dict):
        return {key: _rewrite_env_placeholders(item, respell) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_env_placeholders(item, respell) for item in value]
    return value


def _claude_mcp_flags(block: dict) -> list[str]:
    """Canonical ``mcpServers`` → claude's ``--mcp-config '<json>'`` flag pair (or ``[]``).

    Claude Code's own stdio dialect *is* the canonical one (``command`` / ``args`` / ``env``
    / ``cwd``), so those entries pass through untouched; a remote entry becomes
    ``{type, url, headers}``. ``${VAR}`` is left verbatim — Claude Code expands it against
    its own environment. ``--strict-mcp-config`` is deliberately not passed, so a
    provider-declared server is *additive* to the user's own MCP configuration rather than
    replacing it.
    """
    servers = _mcp_servers(block)
    if not servers:
        return []
    document: dict = {}
    for name, entry in servers.items():
        url = str(entry.get("url") or "").strip()
        if url:
            transport = str(entry.get("transport") or "http").strip()
            if transport not in ("http", "sse"):
                raise ProviderError(
                    f"`mcpServers.{name}.transport` must be http or sse, got {transport!r}"
                )
            server: dict = {"type": transport, "url": url}
            if entry.get("headers"):
                server["headers"] = entry["headers"]
        else:
            server = {"command": str(entry["command"]), "args": entry.get("args") or []}
            for key in ("env", "cwd"):
                if entry.get(key):
                    server[key] = entry[key]
        document[name] = server
    return ["--mcp-config", json.dumps({"mcpServers": document}, sort_keys=True)]


def _claude_settings_flags(block: dict) -> list[str]:
    """``settings`` → claude's ``--settings '<json>'`` flag pair (or ``[]``).

    Claude Code takes a settings **JSON string** on the command line, exactly as it does for
    ``--mcp-config``, so nothing is written to disk. The document is an *additional* settings
    layer that wins over the user's own ``settings.json`` key-by-key rather than replacing it
    — which is what makes it the right home for a per-launcher default ``model``: the launcher
    pins the model while the user layer's permissions, hooks and statusline still apply.
    ``${VAR}`` is left verbatim, as with ``--mcp-config`` — Claude Code expands it itself.
    """
    settings = block.get("settings")
    if settings is None:
        return []
    if not isinstance(settings, dict):
        raise ProviderError("`settings` must be a JSON object")
    if not settings:
        return []
    return ["--settings", json.dumps(settings, sort_keys=True)]


def _opencode_mcp_block(block: dict) -> dict:
    """Canonical ``mcpServers`` → opencode's ``mcp`` config block (or ``{}``).

    opencode's dialect diverges from the canonical one in three ways: ``command`` is a
    single array (binary followed by its args), the stdio env key is ``environment``, and
    every entry carries an explicit ``type`` and ``enabled``. ``${VAR}`` is respelled to
    opencode's own ``{env:VAR}``, which it resolves from the process environment at load.
    """
    servers = _mcp_servers(block)
    result: dict = {}
    for name, entry in servers.items():
        url = str(entry.get("url") or "").strip()
        if url:
            server: dict = {"type": "remote", "url": url, "enabled": True}
            if entry.get("headers"):
                server["headers"] = entry["headers"]
        else:
            args = [str(arg) for arg in entry.get("args") or []]
            server = {
                "type": "local",
                "command": [str(entry["command"]), *args],
                "enabled": True,
            }
            if entry.get("env"):
                server["environment"] = entry["env"]
            if entry.get("cwd"):
                server["cwd"] = entry["cwd"]
        result[name] = _rewrite_env_placeholders(server, lambda var: f"{{env:{var}}}")
    return result


# ---------------------------------------------------------------------------
# per-harness env/command builders -> (env_to_set, env_to_unset, base_command, config_files)
# ---------------------------------------------------------------------------


def _claude_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    base_url = str(block.get("baseUrl") or "").strip()
    command = ["claude", *_claude_mcp_flags(block), *_claude_settings_flags(block)]
    if not base_url:
        # A proxy option without a baseUrl has nothing to sit in front of — fail loudly
        # rather than silently run vanilla Claude against the real Anthropic API.
        if block.get("foldSystemMessages") is True or str(block.get("upstreamApi") or "").strip():
            raise ProviderError(
                "claude config sets a proxy option (foldSystemMessages / upstreamApi) but no "
                "baseUrl for it to proxy"
            )
        # Native Claude: no endpoint/auth/model env to set. The --mcp-config and --settings
        # flags built above still ride along — they are config, not provider overrides.
        return {}, [], command, ()
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

    return env, unset, command, ()


# The provider name agedum assigns to a generated custom-endpoint kimi provider in the
# injected Kimi Code config.toml. The user never types it — agedum selects the model alias
# (whose provider is this) via --model, and default_model points at it too.
KIMI_PROVIDER_NAME = "agedum"


# The per-model knobs a kimi launcher may set — at the top level for a single-model config,
# or inside each `models` entry when the launcher declares several.
_KIMI_MODEL_KEYS = ("contextWindow", "capabilities", "supportEfforts", "defaultEffort")

# Kimi Code gates subagent tiering behind this experimental flag id (default off); agedum
# enables it in the generated config whenever a launcher sets `subagentModel`.
KIMI_SECONDARY_MODEL_FLAG = "secondary-model"


def _kimi_support_efforts(entry: dict) -> list[str]:
    """The efforts one model entry declares (Kimi Code's ``support_efforts``), or ``[]``."""
    values = entry.get("supportEfforts")
    return [str(value) for value in values] if isinstance(values, list) and values else []


def _kimi_models(block: dict, model: str) -> dict[str, dict]:
    """Resolve the declared model entries, default model first.

    Without a ``models`` map a launcher declares exactly one model: the top-level ``model``
    carrying the top-level per-model knobs. With one, every knob belongs to an entry — a
    top-level knob would then apply to no model, so it is rejected rather than dropped.
    """
    declared = block.get("models")
    if declared is None:
        return {model: {key: block[key] for key in _KIMI_MODEL_KEYS if key in block}}
    if not isinstance(declared, dict) or not declared:
        raise ProviderError("kimi `models` must be a non-empty object keyed by model id")
    for name, entry in declared.items():
        if not isinstance(entry, dict):
            raise ProviderError(f"kimi `models.{name}` must be an object of per-model settings")
    stray = [key for key in _KIMI_MODEL_KEYS if key in block]
    if stray:
        raise ProviderError(
            f"kimi config sets `models` and top-level {', '.join(stray)}; move those into the "
            "matching `models` entry — a top-level knob applies to no model once `models` is set"
        )
    if model not in declared:
        raise ProviderError(
            f"kimi `model` {model!r} is not declared in `models` ({', '.join(declared)}); "
            "the default model needs its own entry"
        )
    return {model: declared[model], **{k: v for k, v in declared.items() if k != model}}


def _kimi_model_lines(alias: str, entry: dict) -> list[str]:
    """Render one ``[models."<id>"]`` table from a per-model entry."""
    context_window = entry.get("contextWindow")
    max_context_size = (
        int(context_window)
        if isinstance(context_window, (int, float)) and int(context_window) > 0
        else 262144
    )
    capabilities = entry.get("capabilities")
    model_capabilities = (
        [str(capability) for capability in capabilities]
        if isinstance(capabilities, list) and capabilities
        else ["thinking"]
    )
    caps = ", ".join(f'"{_toml_escape(capability)}"' for capability in model_capabilities)

    lines = [
        f'[models."{_toml_escape(alias)}"]',
        f'provider = "{KIMI_PROVIDER_NAME}"',
        f'model = "{_toml_escape(alias)}"',
        f"max_context_size = {max_context_size}",
        f"capabilities = [{caps}]",
    ]
    support_efforts = _kimi_support_efforts(entry)
    if support_efforts:
        listed_efforts = ", ".join(
            f'"{_toml_escape(effort_value)}"' for effort_value in support_efforts
        )
        lines.append(f"support_efforts = [{listed_efforts}]")
    default_effort = str(entry.get("defaultEffort") or "").strip()
    if default_effort:
        lines.append(f'default_effort = "{_toml_escape(default_effort)}"')
    return lines


def _kimi_config_toml(
    block: dict, base_url: str, model: str, secret_env: str, base_env: dict[str, str]
) -> str:
    """Build a self-sufficient Kimi Code ``config.toml`` pointing at a custom endpoint.

    Kimi Code has no ``--config-file`` flag and its config does not interpolate ``$ENV``, so
    agedum binds this generated ``config.toml`` over ``~/.kimi-code/config.toml`` (the file
    Kimi reads) with the resolved API key baked in (masked in ``--dry-run``). It carries one
    provider, one or more models and toggles thinking; Kimi fills every other setting from its
    own defaults. ``providerType`` must name a Kimi Code provider type (``openai`` for an
    OpenAI Chat Completions surface, ``anthropic``, ``kimi``, …).

    ``effortLevel`` becomes ``[thinking] effort``, and ``supportEfforts`` / ``defaultEffort``
    become the model block's ``support_efforts`` / ``default_effort`` — the roster fields Kimi
    Code resolves an effort against (a model's ``/models`` entry reports them under
    ``think_efforts``).

    A ``models`` map declares several models on the one provider; ``model`` names the default
    (``default_model``) and ``subagentModel`` / ``subagentEffort`` point ``[secondary_model]``
    at the tier subagents run on, so a launcher can pair a wide primary with a cheaper
    subagent model.
    """
    provider_type = str(block.get("providerType") or "openai").strip() or "openai"
    models = _kimi_models(block, model)
    effort = str(block.get("effortLevel") or "").strip()

    # On the kimi wire protocol Kimi Code resolves the configured effort against the model's
    # `support_efforts`: an unlisted effort raises MODEL_CONFIG_INVALID at launch, and an
    # *empty* list silently collapses the effort to plain `on`. Both read as "configured"
    # while doing something else — fail loudly instead. `[thinking] effort` applies to the
    # session's model, so it is the default model's entry that has to list it; a model reached
    # by switching later is Kimi Code's own check at switch time.
    primary_efforts = _kimi_support_efforts(models[model])
    if effort and provider_type == "kimi":
        if not primary_efforts:
            raise ProviderError(
                "kimi `effortLevel` needs `supportEfforts` listing the efforts the model "
                "accepts; without it Kimi Code normalises the effort away to plain `on`"
            )
        if effort not in primary_efforts:
            raise ProviderError(
                f"kimi `effortLevel` {effort!r} is not listed in `supportEfforts` "
                f"({', '.join(primary_efforts)}); Kimi Code rejects an unlisted effort"
            )

    subagent_model = str(block.get("subagentModel") or "").strip()
    subagent_effort = str(block.get("subagentEffort") or "").strip()
    if subagent_effort and not subagent_model:
        raise ProviderError(
            "kimi `subagentEffort` needs `subagentModel`; the effort rides the "
            "`[secondary_model]` entry"
        )
    if subagent_model:
        if subagent_model not in models:
            raise ProviderError(
                f"kimi `subagentModel` {subagent_model!r} is not declared in `models` "
                f"({', '.join(models)}); Kimi Code fails subagent spawning when "
                "`[secondary_model].model` names no `[models]` entry"
            )
        subagent_efforts = _kimi_support_efforts(models[subagent_model])
        if subagent_effort and subagent_effort not in subagent_efforts:
            listed = ", ".join(subagent_efforts) or "none"
            raise ProviderError(
                f"kimi `subagentEffort` {subagent_effort!r} is not listed in the "
                f"`supportEfforts` of model {subagent_model!r} ({listed}); Kimi Code rejects "
                "an unlisted secondary effort"
            )

    lines = [f'default_model = "{_toml_escape(model)}"']
    for alias, entry in models.items():
        lines += ["", *_kimi_model_lines(alias, entry)]
    if subagent_model:
        lines += ["", "[secondary_model]", f'model = "{_toml_escape(subagent_model)}"']
        if subagent_effort:
            lines.append(f'default_effort = "{_toml_escape(subagent_effort)}"')
        # Subagent tiering is an experimental Kimi Code flag, off by default: without the
        # override `[secondary_model]` parses fine and is simply never consulted. The config
        # `experimental` record is keyed by flag id and is the self-contained seam (the
        # KIMI_CODE_EXPERIMENTAL_SECONDARY_MODEL env var still wins over it at runtime).
        lines += ["", "[experimental]", f"{KIMI_SECONDARY_MODEL_FLAG} = true"]
    lines += [
        "",
        f'[providers."{KIMI_PROVIDER_NAME}"]',
        f'type = "{_toml_escape(provider_type)}"',
        f'base_url = "{_toml_escape(base_url)}"',
        f'api_key = "{_toml_escape(base_env.get(secret_env, ""))}"',
    ]
    thinking = block.get("thinking")
    if thinking is not None or effort:
        lines += ["", "[thinking]"]
        if thinking is not None:
            lines.append(f"enabled = {'true' if thinking else 'false'}")
        if effort:
            lines.append(f'effort = "{_toml_escape(effort)}"')
    return "\n".join(lines) + "\n"


def _kimi_mcp_json(mcp_servers: dict) -> str:
    """Build the Kimi Code ``mcp.json`` document from an ``mcpServers`` block."""
    return json.dumps({"mcpServers": mcp_servers}, indent=2) + "\n"


def _kimi_data_dir(base_url: str, model: str) -> Path:
    """The isolated Kimi Code data dir (``KIMI_CODE_HOME``) for one custom-endpoint launcher.

    Kimi Code refreshes its provider-model catalogue at startup and persists it by writing a
    temp file and **renaming it over** ``config.toml``. A rename cannot replace a bind mount,
    so a read-only bind there does not merely reject the write — it fails with ``EBUSY`` and
    the harness reports ``Skipped refreshing <provider>`` on every launch. Seeding the
    generated docs into a dir agedum owns lets that rewrite land, leaves the user's own
    ``~/.kimi-code`` untouched, and still keeps the launcher authoritative: agedum re-seeds
    every launch, so whatever Kimi discovered in-session is replaced by the declared config.

    Derived from endpoint + model so repeat launches reuse the same dir (and its injected
    skills / session history). Lives under ``~/.cache`` so the conception sandbox's writable
    set already covers Kimi's own session, log and update writes.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{base_url}-{model}".lower()).strip("-") or "endpoint"
    return Path.home() / ".cache" / "agedum" / "kimi" / slug


def _kimi_env(block: dict, secret_env: str, base_env: dict[str, str]) -> BuilderResult:
    # Kimi Code's provider/model knobs are appended CLI flags plus a generated config.toml,
    # not env vars. The token (secret_env) reaches the child via the required-env export in
    # build_launch and is also baked into the generated config.
    # The CLI binary is overridable; current Kimi Code packages expose `kimi`.
    binary = str(block.get("binary") or "kimi").strip() or "kimi"
    command = [binary]
    model = str(block.get("model") or "").strip()
    base_url = str(block.get("baseUrl") or "").strip()
    env: dict[str, str] = {}
    config_files: list[ConfigFile] = []
    # A generated config moves the whole Kimi home to a dir agedum owns (see _kimi_data_dir);
    # without one, Kimi Code's own ~/.kimi-code is the target and the docs are bound.
    data_dir = _kimi_data_dir(base_url, model) if base_url else kimi_config_dir()
    seeded = bool(base_url)

    if base_url:
        # Kimi Code has no --base-url flag and its config does not interpolate $ENV, so a
        # custom OpenAI-/Anthropic-compatible endpoint becomes a generated config.toml
        # (provider `agedum` + model, resolved key baked in — like opencode's
        # OPENCODE_CONFIG_CONTENT). There is no --config-file either: Kimi reads config.toml
        # from its data dir, so agedum points KIMI_CODE_HOME at an isolated one and seeds the
        # doc there. `kimi_config_dir()` reads that same env var, so the instruction and skill
        # binds follow it (cli.main applies launch.env before compiling for exactly this).
        if not model:
            raise ProviderError(
                "kimi config sets `baseUrl` but no `model`; set `model` to the upstream model "
                "id served at that endpoint"
            )
        if not secret_env:
            raise ProviderError("kimi config has a baseUrl but no secretEnv to supply the API key")
        config_toml = _kimi_config_toml(block, base_url, model, secret_env, base_env)
        env["KIMI_CODE_HOME"] = str(data_dir)
        config_files.append((str(data_dir / "config.toml"), config_toml, False, True))

    # Kimi Code reads MCP servers from `mcp.json`, not config.toml, so this is a second
    # generated doc rather than another section. Never merged, so the launcher declares its own
    # server set rather than inheriting whatever the host happens to carry — seeded into the
    # isolated home alongside config.toml, or bound read-only when there is no generated config.
    mcp_servers = block.get("mcpServers")
    if mcp_servers:
        if not isinstance(mcp_servers, dict):
            raise ProviderError("kimi `mcpServers` must be an object keyed by server name")
        # claude and opencode respell `${VAR}` into their own expansion syntax; Kimi Code is
        # not known to expand anything in mcp.json, so the placeholder would reach the server
        # as a literal. A shared MCP base extended by a kimi launcher must fail here rather
        # than silently hand the server the string `${TOKEN}`.
        for server_name, entry in mcp_servers.items():
            if _ENV_PLACEHOLDER.search(json.dumps(entry)):
                raise ProviderError(
                    f"kimi `mcpServers.{server_name}` uses a `${{VAR}}` placeholder, which Kimi "
                    "Code is not known to expand in mcp.json; use `bearerTokenEnvVar` for a "
                    "remote token, or write the value literally"
                )
        mcp_path = str(data_dir / "mcp.json")
        mcp_doc = _kimi_mcp_json(mcp_servers)
        config_files.append(
            (mcp_path, mcp_doc, False, True) if seeded else (mcp_path, mcp_doc, False)
        )

    if model:
        command += ["--model", model]
    if block.get("plan") is True:
        command.append("--plan")
    if block.get("yolo") is True:
        command.append("--yolo")
    return env, [], command, tuple(config_files)


def _opencode_env(
    block: dict,
    secret_env: str,
    base_env: dict[str, str],
    *,
    failover: FailoverPlan | None = None,
) -> BuilderResult:
    env: dict[str, str] = {}
    if block.get("disableExternalSkills") is True:
        env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
    document = _opencode_config_doc(block)
    for provider_def in _provider_defs(block.get("providerDef")):
        document = _apply_provider_def(document, provider_def, base_env)
    if failover is not None:
        document = _apply_failover_routes(document, failover)
    if document:
        # Key order is semantic, never cosmetic: opencode evaluates a permission map in
        # config key insertion order and keeps the *last* matching rule, so the shipped
        # `{"*": "deny", "git log*": "allow", …, "*|*": "deny"}` shape relies on its
        # trailing guards being read after the allow-list. Sorting the keys here moved
        # every `*…` guard ahead of the alphabetic allows and silently inverted that —
        # `git log … | sh` matched the allow last and was permitted. Emit authored order.
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(document)
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


def _cline_providers_doc(
    base_url: str, model: str, context_window: int | None, max_tokens: int | None
) -> dict:
    """A single-provider cline ``providers.json`` for a custom OpenAI-compatible endpoint.

    Only structure lives here — provider id, base URL, model. The API key is **not** written:
    it rides the runtime ``--key`` flag (masked in ``--dry-run``), cline's documented
    mechanism, so no secret lands on disk. ``lastUsedProvider`` makes cline select the
    provider without a ``--provider`` flag, which would otherwise rebuild the provider from
    CLI flags and silently drop the stored ``baseUrl`` (posting to the OpenAI default).

    ``context_window`` / ``max_tokens`` (when given) become a one-entry ``models`` array —
    cline's generic ``openai-compatible`` provider has no model catalogue, so this is how it
    learns the model's window (its ``X/N`` meter + the point agentic compaction fires) and
    output cap. Omitted → cline falls back to its built-in default window.
    """
    settings = {
        "provider": CLINE_OPENAI_PROVIDER,
        "apiKey": "",
        "model": model,
        "baseUrl": base_url,
    }
    if context_window is not None or max_tokens is not None:
        model_info: dict = {"id": model}
        if context_window is not None:
            model_info["contextWindow"] = context_window
        if max_tokens is not None:
            model_info["maxTokens"] = max_tokens
        settings["models"] = [model_info]
    return {
        "version": 1,
        "lastUsedProvider": CLINE_OPENAI_PROVIDER,
        "providers": {
            CLINE_OPENAI_PROVIDER: {
                "settings": settings,
                "updatedAt": "2020-01-01T00:00:00.000Z",
                "tokenSource": "manual",
            }
        },
    }


def _cline_positive_int(block: dict, key: str) -> int | None:
    """Read a positive-int config field (``contextWindow`` / ``maxTokens``); reject junk."""
    value = block.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) <= 0:
        raise ProviderError(f"cline config `{key}` must be a positive integer, got {value!r}")
    return int(value)


def _cline_compaction_flags(block: dict) -> list[str]:
    """``compaction`` → cline's ``--compaction <mode>`` (``agentic`` = LLM summarizer, ``basic``
    = the built-in default, ``off``). Absent leaves cline's own default (``basic``)."""
    mode = str(block.get("compaction") or "").strip()
    if not mode:
        return []
    if mode not in ("agentic", "basic", "off"):
        raise ProviderError(f"cline config `compaction` must be agentic|basic|off, got {mode!r}")
    return ["--compaction", mode]


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
        context_window = _cline_positive_int(block, "contextWindow")
        max_tokens = _cline_positive_int(block, "maxTokens")
        data_dir = _cline_data_dir(base_url, model)
        config_path = str(data_dir / "settings" / "providers.json")
        config_doc = (
            json.dumps(_cline_providers_doc(base_url, model, context_window, max_tokens), indent=2)
            + "\n"
        )
        command = ["cline"]
        if effort:
            command += ["--thinking", effort]
        command += _cline_compaction_flags(block)
        if block.get("plan") is True:
            command.append("--plan")
        command += _cline_auto_approve_flags(block)
        token = base_env.get(secret_env, "")
        if token:
            command += ["--key", token]
        # `writable=True`: seed providers.json into the (already writable) CLINE_DATA_DIR
        # rather than read-only binding it — cline rewrites this file to persist its provider
        # selection, and a ro-bind makes that write fail with EROFS. agedum re-seeds it every
        # launch, so cline's in-session edits are transient (the correct baseUrl wins next run).
        return (
            {"CLINE_DATA_DIR": str(data_dir)},
            [],
            command,
            ((config_path, config_doc, False, True),),
        )

    command = ["cline"]
    if model:
        command += ["--model", model]
    provider = str(block.get("provider") or "").strip()
    if provider:
        command += ["--provider", provider]
    if effort:
        command += ["--thinking", effort]
    command += _cline_compaction_flags(block)
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


def _toml_config_value(value: object) -> str:
    """Render a config value for a codex ``-c key=<value>`` override: scalars via
    :func:`_toml_scalar` (bool/int bare, else quoted), lists as TOML arrays, dicts as
    inline tables."""
    if isinstance(value, list):
        return "[" + ", ".join(_toml_config_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f'"{_toml_escape(str(key))}" = {_toml_config_value(item)}'
                for key, item in value.items()
            )
            + "}"
        )
    return _toml_scalar(value)


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

    # Canonical `mcpServers`, translated into opencode's `mcp` dialect. Merged before the
    # passthrough below, so an explicit `opencodeConfig.mcp` entry still wins on conflict.
    mcp = _opencode_mcp_block(block)
    if mcp:
        document["mcp"] = mcp

    # `opencodeConfig` is a literal opencode config object, deep-merged into the
    # document last so it wins on conflict with the modeled keys — the escape hatch
    # for any opencode option agedum does not model, written in opencode's own format.
    passthrough = block.get("opencodeConfig")
    if passthrough is not None:
        if not isinstance(passthrough, dict):
            raise ProviderError("opencodeConfig must be a JSON object")
        document = _deep_merge(document, passthrough)

    # `agentAppend` (inside an `opencodeConfig.agent.<name>` block, beside `prompt`):
    # role-specific instructions declared apart from the narrative `prompt` — e.g. a workflow
    # handoff rule — folded onto the end of that agent's prompt here, so opencode receives a
    # single `prompt` and never sees the synthetic key. Resolved after the passthrough merge, so
    # any `extends`-inherited value is already merged in. A string or list of strings (blank-line
    # joined); an explicit `null` (an `extends` child clearing an inherited append) folds nothing
    # but still strips the key. Absent → prompt unchanged. opencode is the only harness with
    # per-agent prompts in the provider config, so this field is opencode-only.
    agent_block = document.get("agent")
    if isinstance(agent_block, dict) and any(
        isinstance(entry, dict) and "agentAppend" in entry for entry in agent_block.values()
    ):
        # Rebuild the agent map, copying only the entries we touch: build_launch must not mutate
        # the caller's config, and `_deep_merge` aliases the passthrough's `agent` sub-dicts by
        # reference (its else-branch), so popping/rewriting in place would edit the input.
        folded: dict = {}
        for name, entry in agent_block.items():
            if isinstance(entry, dict) and "agentAppend" in entry:
                append_text = _opencode_agent_append(entry["agentAppend"])
                entry = {key: value for key, value in entry.items() if key != "agentAppend"}
                if append_text:
                    entry["prompt"] = _opencode_join_prompt(entry.get("prompt"), append_text)
            folded[name] = entry
        document["agent"] = folded

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


def _opencode_agent_append(value: object) -> str:
    """Normalise an opencode agent's ``agentAppend`` into the markdown appended to its prompt.

    A string is trimmed; a list of strings is trimmed per entry and joined with a blank line
    between entries so each block keeps its own heading; ``null`` — an ``extends`` child clearing
    an inherited append — and empty / whitespace-only entries yield ``""`` (no append). Any other
    type (or a non-string list entry) raises :class:`ProviderError`.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        blocks: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ProviderError("opencode `agentAppend` list entries must be strings")
            if item.strip():
                blocks.append(item.strip())
        return "\n\n".join(blocks)
    raise ProviderError("opencode `agentAppend` must be a string, a list of strings, or null")


def _opencode_join_prompt(prompt: object, append_text: str) -> str:
    """Join an agent's existing prompt with resolved ``agentAppend`` text (one blank line).

    A missing prompt yields the append alone; a string prompt is trimmed and separated from the
    append by a blank line. A non-string prompt (a malformed opencode agent config) raises
    rather than being coerced to its Python ``repr``.
    """
    if prompt is None:
        return append_text
    if isinstance(prompt, str):
        base = prompt.strip()
        return f"{base}\n\n{append_text}" if base else append_text
    raise ProviderError("opencode `agentAppend` requires the agent's `prompt` to be a string")


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


def _codex_mcp_overrides(block: dict) -> list[tuple[str, object]]:
    """Canonical ``mcpServers`` → codex ``-c mcp_servers.<name>…`` override pairs.

    codex reads its MCP servers from config (``[mcp_servers.<name>]`` tables), so each
    server becomes one dotted-key override per field — ``command`` / ``args`` / ``env.*`` /
    ``cwd`` for a stdio entry, ``url`` / ``headers`` for a remote one — merged onto
    ``~/.codex/config.toml`` at launch, exactly like the endpoint ``model_providers``
    overrides. ``${VAR}`` placeholders are rejected: codex is not known to expand them in
    config values (the kimi precedent), and a literal token must never reach a ``-c`` arg.
    """
    servers = _mcp_servers(block)
    overrides: list[tuple[str, object]] = []
    for name, entry in servers.items():
        if _ENV_PLACEHOLDER.search(json.dumps(entry)):
            raise ProviderError(
                f"codex `mcpServers.{name}` uses a `${{VAR}}` placeholder, which codex is "
                "not known to expand in its config; write the value literally"
            )
        prefix = f"mcp_servers.{name}"
        command = str(entry.get("command") or "").strip()
        if command:
            overrides.append((f"{prefix}.command", command))
            args = entry.get("args") or []
            if args:
                overrides.append((f"{prefix}.args", [str(arg) for arg in args]))
            env = entry.get("env") or {}
            if not isinstance(env, dict):
                raise ProviderError(f"`mcpServers.{name}.env` must be an object")
            for key, value in env.items():
                overrides.append((f"{prefix}.env.{key}", value))
            if entry.get("cwd"):
                overrides.append((f"{prefix}.cwd", entry["cwd"]))
        else:
            overrides.append((f"{prefix}.url", entry["url"]))
            headers = entry.get("headers") or {}
            if not isinstance(headers, dict):
                raise ProviderError(f"`mcpServers.{name}.headers` must be an object")
            if headers:
                overrides.append((f"{prefix}.headers", dict(headers)))
    return overrides


def _flatten_codex_config(codex_config: dict) -> list[tuple[str, object]]:
    """Flatten a ``codexConfig`` table into dotted-key override pairs.

    Nested tables become dotted keys (``sandbox_workspace_write.writable_roots``) — the
    same dotted-key TOML shape the ``mcp_servers`` overrides use, so codex merges them
    onto ``~/.codex/config.toml`` exactly like the written table.
    """
    overrides: list[tuple[str, object]] = []

    def walk(prefix: str, value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}", item)
        else:
            overrides.append((prefix, value))

    for key, value in codex_config.items():
        walk(key, value)
    return overrides


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
        for key, value in _flatten_codex_config(codex_config):
            command += ["-c", f"{key}={_toml_config_value(value)}"]

    # Canonical `mcpServers`, translated into codex's config dialect as `-c mcp_servers.<name>…`
    # overrides — the same `-c` mechanism as the endpoint and codexConfig overrides above.
    for key, value in _codex_mcp_overrides(block):
        command += ["-c", f"{key}={_toml_config_value(value)}"]

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
