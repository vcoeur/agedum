import functools
import json
import tomllib
from pathlib import Path

import pytest
import yaml_fleet_fixtures as fleet

from agedum.provider import (
    Launch,
    ModelCatalogSchemaError,
    ProviderError,
    ProviderSchemaError,
    YamlBooleanTrapError,
    build_launch,
    default_env_file,
    expand_model_refs,
    list_providers,
    load_config,
    load_config_with_format,
    load_merged_config,
    load_merged_config_with_format,
    load_model_catalog,
    merge_json_onto_file,
    parse_env_file,
    providers_dir,
    required_env,
    resolve_config_path,
    with_prompt,
)


def _write_config(root, rel, obj):
    """Write a JSON config at ``root/rel`` (creating parents); return its path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


# --- config-path resolution (anchored at the providers root) ---


def test_resolve_name_under_providers_root(tmp_path):
    assert resolve_config_path("ds-auto", tmp_path) == tmp_path / "ds-auto.json"


def test_resolve_nested_path_is_providers_root_relative(tmp_path):
    # A value with a slash is providers-root-relative (not CWD-relative); .json appended.
    nested = tmp_path / "claude" / "deepseek.json"
    assert resolve_config_path("claude/deepseek", tmp_path) == nested
    assert resolve_config_path("claude/deepseek.json", tmp_path) == nested
    assert resolve_config_path("base/claude.json", tmp_path) == tmp_path / "base" / "claude.json"


def test_resolve_absolute_path(tmp_path):
    assert resolve_config_path("/abs/p.json", tmp_path) == Path("/abs/p.json")
    assert resolve_config_path("/abs/p", tmp_path) == Path("/abs/p.json")


def test_providers_dir_env_override(monkeypatch):
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", "/custom/providers")
    assert str(providers_dir()) == "/custom/providers"


def test_default_env_file_override(monkeypatch):
    monkeypatch.setenv("AGENTS_ENV_FILE", "/custom/.env")
    assert str(default_env_file()) == "/custom/.env"


# --- env-file parsing ---


def test_parse_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "DEEPSEEK_API_KEY=sk-abc123\n"
        'export QUOTED="with spaces"\n'
        "SINGLE='single'\n"
        "  SPACED  =  trimmed  \n"
    )
    parsed = parse_env_file(env)
    assert parsed == {
        "DEEPSEEK_API_KEY": "sk-abc123",
        "QUOTED": "with spaces",
        "SINGLE": "single",
        "SPACED": "trimmed",
    }


def test_parse_env_file_strips_trailing_comment_from_unquoted_value(tmp_path):
    # `source` semantics: `KEY=val # comment` sets "val"; a `#` inside the value
    # (`val#ue`) or inside quotes is part of the value.
    env = tmp_path / ".env"
    env.write_text(
        'COMMENTED=value # the comment\nHASH_INSIDE=val#ue\nQUOTED_HASH="value # kept"\n'
    )
    parsed = parse_env_file(env)
    assert parsed == {
        "COMMENTED": "value",
        "HASH_INSIDE": "val#ue",
        "QUOTED_HASH": "value # kept",
    }


# --- config loading ---


def test_load_config_rejects_non_object(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("[1, 2]")
    with pytest.raises(ProviderError, match="must be a JSON object"):
        load_config(bad)


def test_load_config_invalid_json(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("{ not json")
    with pytest.raises(ProviderError, match="invalid JSON"):
        load_config(bad)


# --- provider listing ---


def test_list_providers_summarises_name_harness_model(tmp_path):
    (tmp_path / "claude-ds.json").write_text(
        json.dumps({"harness": "claude", "config": {"model": "deepseek-v4-pro"}})
    )
    (tmp_path / "kimi.json").write_text(json.dumps({"harness": "kimi"}))
    summaries = list_providers(tmp_path)
    assert [(s.name, s.harness, s.model) for s in summaries] == [
        ("claude-ds", "claude", "deepseek-v4-pro"),
        ("kimi", "kimi", None),
    ]


def test_list_providers_falls_back_to_the_settings_model(tmp_path):
    # A native claude launcher pins its model through config.settings; without the fallback
    # the roster would report it as model-less.
    (tmp_path / "opus.json").write_text(
        json.dumps({"harness": "claude", "config": {"settings": {"model": "opus"}}})
    )
    (summary,) = list_providers(tmp_path)
    assert summary.model == "opus"


def test_list_providers_prefers_config_model_over_the_settings_model(tmp_path):
    (tmp_path / "x.json").write_text(
        json.dumps(
            {"harness": "claude", "config": {"model": "env-model", "settings": {"model": "opus"}}}
        )
    )
    (summary,) = list_providers(tmp_path)
    assert summary.model == "env-model"


def test_list_providers_sorted_by_name(tmp_path):
    for name in ("zeta", "alpha", "mid"):
        (tmp_path / f"{name}.json").write_text(json.dumps({"harness": "kimi"}))
    assert [s.name for s in list_providers(tmp_path)] == ["alpha", "mid", "zeta"]


def test_list_providers_invalid_config_carries_error_not_raise(tmp_path):
    (tmp_path / "broken.json").write_text("{ not json")
    (summary,) = list_providers(tmp_path)
    assert summary.name == "broken"
    assert summary.harness is None and summary.model is None
    assert summary.error is not None


def test_list_providers_missing_dir_is_empty(tmp_path):
    assert list_providers(tmp_path / "absent") == []


def test_list_providers_defaults_to_providers_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(tmp_path))
    (tmp_path / "x.json").write_text(json.dumps({"harness": "opencode", "config": {"model": "m"}}))
    assert [(s.name, s.harness, s.model) for s in list_providers()] == [("x", "opencode", "m")]


# --- required-env validation ---


def test_required_env_list_plus_secret():
    config = {
        "harness": "opencode",
        "secretEnv": "DEEPSEEK_API_KEY",
        "requiredEnv": ["DEEPSEEK_API_KEY", "OPENROUTER_KEY"],
        "config": {},
    }
    assert required_env(config) == ["DEEPSEEK_API_KEY", "OPENROUTER_KEY"]


def test_missing_required_env_raises():
    config = {"harness": "kimi", "secretEnv": "KIMI_API_KEY", "config": {}}
    with pytest.raises(ProviderError, match="KIMI_API_KEY is required .* but is not set"):
        build_launch(config, base_env={})


def test_empty_required_env_value_raises():
    config = {"harness": "kimi", "secretEnv": "KIMI_API_KEY", "config": {}}
    with pytest.raises(ProviderError, match="is not set"):
        build_launch(config, base_env={"KIMI_API_KEY": ""})


def test_unknown_harness_errors():
    with pytest.raises(ProviderError, match="harness"):
        build_launch({"harness": "agentsconf", "config": {}}, base_env={})


# --- claude env mapping (parity with the retired build-script) ---

_DEEPSEEK_ENV = {"DEEPSEEK_API_KEY": "sk-secret"}


def test_claude_full_mapping():
    launch = build_launch(
        {
            "harness": "claude",
            "slug": "claude-deepseek-auto",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/anthropic",
                "authStyle": "bearer",
                "model": "deepseek-v4-pro",
                "smallFastModel": "deepseek-v4-flash",
                "haikuAlias": "deepseek-v4-flash",
                "sonnetAlias": "deepseek-v4-pro",
                "opusAlias": "deepseek-v4-pro",
                "subagentModel": "deepseek-v4-pro",
                "maxContextTokens": 1000000,
                "effortLevel": "max",
                "disable1M": True,
                "disableTelemetry": True,
                "disableCaching": False,
            },
        },
        base_env=_DEEPSEEK_ENV,
    )
    env = launch.env
    assert env["DEEPSEEK_API_KEY"] == "sk-secret"  # required var exported verbatim
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"  # secret resolved into the token
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" in launch.unset
    assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "deepseek-v4-flash"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-v4-flash"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-pro"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] == "1"
    assert env["DISABLE_TELEMETRY"] == "1"
    assert "DISABLE_PROMPT_CACHING" not in env  # false -> omitted
    assert "CLAUDE_CODE_USE_BEDROCK" in launch.unset
    assert launch.command == ["claude"]
    # secret values are flagged for masking
    assert "DEEPSEEK_API_KEY" in launch.secrets
    assert "ANTHROPIC_AUTH_TOKEN" in launch.secrets


def test_claude_fold_system_messages_flag():
    launch = build_launch(
        {
            "harness": "claude",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/anthropic",
                "model": "deepseek-v4-pro",
                "foldSystemMessages": True,
            },
        },
        base_env=_DEEPSEEK_ENV,
    )
    assert launch.env["AGEDUM_FOLD_SYSTEM_MESSAGES"] == "1"
    # the upstream URL stays the real endpoint; the proxy is interposed at run time
    assert launch.env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"


def test_claude_fold_system_messages_omitted_when_unset():
    launch = build_launch(
        {
            "harness": "claude",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"baseUrl": "https://api.deepseek.com/anthropic", "model": "m"},
        },
        base_env=_DEEPSEEK_ENV,
    )
    assert "AGEDUM_FOLD_SYSTEM_MESSAGES" not in launch.env


_GO_ENV = {"OPENCODE_GO_API_KEY": "sk-go"}


def test_claude_upstream_api_openai_completions_sets_translate_flag():
    launch = build_launch(
        {
            "harness": "claude",
            "secretEnv": "OPENCODE_GO_API_KEY",
            "config": {
                "baseUrl": "https://opencode.ai/zen/go",
                "authStyle": "apikey",
                "upstreamApi": "openai-completions",
                "model": "kimi-k2.7-code",
                "effortLevel": "high",
            },
        },
        base_env=_GO_ENV,
    )
    assert launch.env["AGEDUM_TRANSLATE_OPENAI"] == "1"
    assert "AGEDUM_FOLD_SYSTEM_MESSAGES" not in launch.env
    # the upstream URL stays the real endpoint; the proxy is interposed at run time
    assert launch.env["ANTHROPIC_BASE_URL"] == "https://opencode.ai/zen/go"
    assert launch.env["ANTHROPIC_MODEL"] == "kimi-k2.7-code"


def test_claude_upstream_api_anthropic_messages_is_noop():
    launch = build_launch(
        {
            "harness": "claude",
            "secretEnv": "OPENCODE_GO_API_KEY",
            "config": {
                "baseUrl": "https://opencode.ai/zen/go",
                "authStyle": "apikey",
                "upstreamApi": "anthropic-messages",
                "model": "kimi-k2.7-code",
            },
        },
        base_env=_GO_ENV,
    )
    assert "AGEDUM_TRANSLATE_OPENAI" not in launch.env


def test_claude_upstream_api_rejects_unknown_value():
    with pytest.raises(ProviderError, match="unknown upstreamApi"):
        build_launch(
            {
                "harness": "claude",
                "secretEnv": "OPENCODE_GO_API_KEY",
                "config": {"baseUrl": "https://opencode.ai/zen/go", "upstreamApi": "grpc"},
            },
            base_env=_GO_ENV,
        )


def test_claude_proxy_option_without_base_url_is_rejected():
    with pytest.raises(ProviderError, match="no .*baseUrl"):
        build_launch(
            {
                "harness": "claude",
                "secretEnv": "OPENCODE_GO_API_KEY",
                "config": {"upstreamApi": "openai-completions", "model": "kimi-k2.7-code"},
            },
            base_env=_GO_ENV,
        )


def test_claude_upstream_api_and_fold_are_mutually_exclusive():
    with pytest.raises(ProviderError, match="both `upstreamApi"):
        build_launch(
            {
                "harness": "claude",
                "secretEnv": "OPENCODE_GO_API_KEY",
                "config": {
                    "baseUrl": "https://opencode.ai/zen/go",
                    "upstreamApi": "openai-completions",
                    "foldSystemMessages": True,
                },
            },
            base_env=_GO_ENV,
        )


# ---------------------------------------------------------------------------
# claude / Kimi Code subscription — endpoint guard + caching/thinking/compact
# ---------------------------------------------------------------------------

_KIMI_ENV = {"KIMI_API_KEY": "sk-kimi-test"}


def _kimi_code_config() -> dict:
    """The claude/kimi-code launcher shape (mirrors agentsconf providers/claude/kimi-code.json)."""
    return {
        "harness": "claude",
        "secretEnv": "KIMI_API_KEY",
        "config": {
            "baseUrl": "https://api.kimi.com/coding",
            "authStyle": "apikey",
            "upstreamApi": "openai-completions",
            "openaiPromptCacheKey": True,
            "openaiThinking": "toggle",
            "model": "kimi-for-coding",
            "maxContextTokens": 262144,
            "autoCompactWindow": 230000,
        },
    }


def test_kimi_code_targets_subscription_endpoint_not_moonshot():
    launch = build_launch(_kimi_code_config(), base_env=_KIMI_ENV)
    base_url = launch.env["ANTHROPIC_BASE_URL"]
    # Must be the Kimi *subscription* (coding) endpoint, never the metered Moonshot platform API.
    assert base_url == "https://api.kimi.com/coding"
    assert "moonshot" not in base_url
    assert launch.env["ANTHROPIC_API_KEY"] == "sk-kimi-test"  # resolved from KIMI_API_KEY
    assert launch.env["ANTHROPIC_MODEL"] == "kimi-for-coding"


def test_kimi_code_enables_cache_thinking_and_translate():
    launch = build_launch(_kimi_code_config(), base_env=_KIMI_ENV)
    assert launch.env["AGEDUM_TRANSLATE_OPENAI"] == "1"
    assert launch.env["AGEDUM_OPENAI_PROMPT_CACHE_KEY"] == "1"
    assert launch.env["AGEDUM_OPENAI_THINKING"] == "toggle"


def test_claude_context_window_env():
    launch = build_launch(_kimi_code_config(), base_env=_KIMI_ENV)
    # auto-compact fires below the max-context ceiling, leaving headroom.
    assert launch.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert launch.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "230000"
    assert int(launch.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]) < int(
        launch.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]
    )


def test_claude_extra_env_passthrough():
    # `extraEnv` is a general escape hatch — arbitrary Claude env vars, stringified, applied last.
    launch = build_launch(
        {
            "harness": "claude",
            "secretEnv": "K",
            "config": {
                "baseUrl": "https://x/y",
                "authStyle": "apikey",
                "model": "m",
                "extraEnv": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": 32768, "FOO": "bar"},
            },
        },
        base_env={"K": "v"},
    )
    assert launch.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32768"  # int value stringified
    assert launch.env["FOO"] == "bar"


def test_claude_openai_thinking_rejects_unknown_mode():
    config = _kimi_code_config()
    config["config"]["openaiThinking"] = "deep"
    with pytest.raises(ProviderError, match="unknown openaiThinking"):
        build_launch(config, base_env=_KIMI_ENV)


def test_claude_openai_cache_option_requires_translate():
    config = _kimi_code_config()
    config["config"].pop("upstreamApi")  # cache/thinking options with no translating proxy
    with pytest.raises(ProviderError, match="OpenAI-translate option"):
        build_launch(config, base_env=_KIMI_ENV)


def test_claude_apikey_auth_style():
    launch = build_launch(
        {
            "harness": "claude",
            "secretEnv": "SOME_KEY",
            "config": {"baseUrl": "https://x/anthropic", "authStyle": "apikey", "model": "m"},
        },
        base_env={"SOME_KEY": "kv"},
    )
    assert launch.env["ANTHROPIC_API_KEY"] == "kv"
    assert "ANTHROPIC_AUTH_TOKEN" in launch.unset


def test_claude_native_runs_bare():
    launch = build_launch(
        {
            "harness": "claude",
            "slug": "claude-native",
            "config": {"baseUrl": "", "model": "", "maxContextTokens": 0, "disable1M": False},
        },
        base_env={},
    )
    assert launch.env == {}  # no requiredEnv, no provider overrides
    assert launch.unset == []
    assert launch.command == ["claude"]


def test_claude_baseurl_without_secret_errors():
    with pytest.raises(ProviderError, match="secretEnv"):
        build_launch(
            {"harness": "claude", "config": {"baseUrl": "https://x/anthropic"}}, base_env={}
        )


# --- canonical mcpServers -> per-harness MCP dialects ---


def _claude_mcp_document(launch):
    """The `--mcp-config` payload claude was launched with."""
    assert "--mcp-config" in launch.command
    return json.loads(launch.command[launch.command.index("--mcp-config") + 1])


def test_claude_mcp_servers_become_an_additive_mcp_config_flag():
    # Claude's stdio dialect is the canonical one, so the entry passes through unchanged —
    # and --strict-mcp-config is never passed, so the user's own servers still load.
    launch = build_launch(
        {
            "harness": "claude",
            "config": {
                "mcpServers": {
                    "nodum": {
                        "command": "nodum",
                        "args": ["mcp", "serve"],
                        "env": {"NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}"},
                    }
                }
            },
        },
        base_env={},
    )
    assert launch.command[0] == "claude"
    assert "--strict-mcp-config" not in launch.command
    assert _claude_mcp_document(launch) == {
        "mcpServers": {
            "nodum": {
                "command": "nodum",
                "args": ["mcp", "serve"],
                # Left verbatim: Claude Code expands ${VAR} itself, so no token in argv.
                "env": {"NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}"},
            }
        }
    }


def test_claude_mcp_servers_reach_a_bare_native_launch():
    # The no-baseUrl path returns early; MCP must survive it (native Claude + MCP is the
    # most likely combination of all).
    launch = build_launch(
        {
            "harness": "claude",
            "config": {"mcpServers": {"nodum": {"command": "nodum", "args": ["mcp", "serve"]}}},
        },
        base_env={},
    )
    assert launch.env == {}
    assert _claude_mcp_document(launch)["mcpServers"]["nodum"]["command"] == "nodum"


def test_claude_mcp_remote_entry_defaults_to_http():
    launch = build_launch(
        {
            "harness": "claude",
            "config": {
                "mcpServers": {
                    "buffer": {
                        "url": "https://mcp.buffer.com/mcp",
                        "headers": {"Authorization": "Bearer ${BUFFER_KEY}"},
                    }
                }
            },
        },
        base_env={},
    )
    assert _claude_mcp_document(launch)["mcpServers"]["buffer"] == {
        "type": "http",
        "url": "https://mcp.buffer.com/mcp",
        "headers": {"Authorization": "Bearer ${BUFFER_KEY}"},
    }


def test_claude_mcp_remote_rejects_an_unknown_transport():
    with pytest.raises(ProviderError, match="transport"):
        build_launch(
            {
                "harness": "claude",
                "config": {"mcpServers": {"x": {"url": "https://x/mcp", "transport": "grpc"}}},
            },
            base_env={},
        )


def test_mcp_entry_cannot_be_both_stdio_and_remote():
    with pytest.raises(ProviderError, match="stdio or remote"):
        build_launch(
            {
                "harness": "claude",
                "config": {"mcpServers": {"x": {"command": "x", "url": "https://x/mcp"}}},
            },
            base_env={},
        )


def test_mcp_entry_needs_a_command_or_a_url():
    with pytest.raises(ProviderError, match="command.*url"):
        build_launch(
            {"harness": "claude", "config": {"mcpServers": {"x": {"args": ["serve"]}}}},
            base_env={},
        )


def test_opencode_mcp_servers_translate_to_the_local_dialect():
    # opencode diverges three ways: command is one array, the env key is `environment`,
    # and ${VAR} is respelled to opencode's own {env:VAR}.
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {
                "mcpServers": {
                    "nodum": {
                        "command": "nodum",
                        "args": ["mcp", "serve"],
                        "env": {"NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}"},
                    }
                }
            },
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["mcp"]["nodum"] == {
        "type": "local",
        "command": ["nodum", "mcp", "serve"],
        "environment": {"NODUM_AGENT_TOKEN": "{env:NODUM_AGENT_TOKEN}"},
        "enabled": True,
    }


def test_opencode_mcp_remote_respells_the_placeholder_in_headers():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {
                "mcpServers": {
                    "buffer": {
                        "url": "https://mcp.buffer.com/mcp",
                        "headers": {"Authorization": "Bearer ${BUFFER_KEY}"},
                    }
                }
            },
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["mcp"]["buffer"] == {
        "type": "remote",
        "url": "https://mcp.buffer.com/mcp",
        "headers": {"Authorization": "Bearer {env:BUFFER_KEY}"},
        "enabled": True,
    }


def test_opencode_mcp_passthrough_wins_over_the_canonical_block():
    # The canonical block is merged before opencodeConfig, so a launcher can still override
    # one server in opencode's own dialect without abandoning the shared base.
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {
                "mcpServers": {"nodum": {"command": "nodum", "args": ["mcp", "serve"]}},
                "opencodeConfig": {"mcp": {"nodum": {"enabled": False}}},
            },
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["mcp"]["nodum"]["enabled"] is False
    assert payload["mcp"]["nodum"]["command"] == ["nodum", "mcp", "serve"]


# --- claude settings layer ---


def _claude_settings_document(launch):
    """The `--settings` payload claude was launched with."""
    assert "--settings" in launch.command
    return json.loads(launch.command[launch.command.index("--settings") + 1])


def test_claude_settings_becomes_a_settings_flag():
    # Claude Code takes a JSON string, so nothing is written to disk — and the layer is
    # additive, so a launcher pins `model` without disturbing the user's own settings.json.
    launch = build_launch(
        {"harness": "claude", "config": {"settings": {"model": "fable"}}},
        base_env={},
    )
    assert launch.command[0] == "claude"
    assert _claude_settings_document(launch) == {"model": "fable"}


def test_claude_settings_reaches_a_bare_native_launch():
    # The no-baseUrl path returns early; the settings layer must survive it — a native
    # launcher pinning its default model is the whole reason the key exists.
    launch = build_launch(
        {"harness": "claude", "config": {"settings": {"model": "opus"}}},
        base_env={},
    )
    assert launch.env == {}
    assert _claude_settings_document(launch) == {"model": "opus"}


def test_claude_settings_rides_alongside_mcp_config():
    launch = build_launch(
        {
            "harness": "claude",
            "config": {
                "mcpServers": {"nodum": {"command": "nodum", "args": ["mcp", "serve"]}},
                "settings": {"model": "fable"},
            },
        },
        base_env={},
    )
    assert _claude_mcp_document(launch)["mcpServers"]["nodum"]["command"] == "nodum"
    assert _claude_settings_document(launch) == {"model": "fable"}


def test_claude_settings_leaves_env_placeholders_verbatim():
    # Same contract as --mcp-config: Claude Code expands ${VAR} against its own environment,
    # so no secret is baked into argv.
    launch = build_launch(
        {"harness": "claude", "config": {"settings": {"apiKeyHelper": "echo ${TOKEN}"}}},
        base_env={},
    )
    assert _claude_settings_document(launch) == {"apiKeyHelper": "echo ${TOKEN}"}


def test_claude_without_settings_adds_no_flag():
    launch = build_launch({"harness": "claude", "config": {}}, base_env={})
    assert launch.command == ["claude"]


def test_claude_empty_settings_adds_no_flag():
    launch = build_launch({"harness": "claude", "config": {"settings": {}}}, base_env={})
    assert launch.command == ["claude"]


def test_claude_settings_rejects_a_non_object():
    with pytest.raises(ProviderError, match="settings"):
        build_launch(
            {"harness": "claude", "config": {"settings": '{"model": "fable"}'}},
            base_env={},
        )


# --- kimi env/command mapping ---


def test_kimi_appends_flags_and_exports_token():
    launch = build_launch(
        {
            "harness": "kimi",
            "secretEnv": "KIMI_API_KEY",
            "config": {"model": "kimi-k2.6", "plan": True, "yolo": True},
        },
        base_env={"KIMI_API_KEY": "kk"},
    )
    assert launch.env["KIMI_API_KEY"] == "kk"  # token reaches the child via required-env
    assert launch.command == ["kimi", "--model", "kimi-k2.6", "--plan", "--yolo"]


def test_kimi_thinking_without_base_url_is_a_noop():
    # thinking now lives in the generated config.toml (needs baseUrl); without an endpoint
    # there is no config to carry it, so the command stays bare — Kimi Code dropped the
    # --thinking / --no-thinking flags.
    launch = build_launch({"harness": "kimi", "config": {"thinking": False}}, base_env={})
    assert launch.command == ["kimi"]
    assert launch.config_files == ()


def test_kimi_yolo_flag():
    launch = build_launch({"harness": "kimi", "config": {"yolo": True}}, base_env={})
    assert launch.command == ["kimi", "--yolo"]


def test_kimi_native_empty_config():
    launch = build_launch({"harness": "kimi", "config": {}}, base_env={})
    assert launch.command == ["kimi"]
    assert launch.env == {}


def test_kimi_base_url_generates_config_toml():
    launch = build_launch(
        {
            "harness": "kimi",
            "secretEnv": "OPENCODE_GO_API_KEY",
            "config": {
                "baseUrl": "https://opencode.ai/zen/go/v1",
                "model": "kimi-k2.7-code",
                "thinking": True,
            },
        },
        base_env={"OPENCODE_GO_API_KEY": "sk-go"},
    )
    # No --config-file / --thinking flag: Kimi Code reads config.toml from its data dir.
    assert launch.command == ["kimi", "--model", "kimi-k2.7-code"]
    assert len(launch.config_files) == 1
    target, content, merge_json, writable = launch.config_files[0]
    assert target == str(Path(launch.env["KIMI_CODE_HOME"]) / "config.toml")
    assert merge_json is False
    assert writable is True
    doc = tomllib.loads(content)
    assert doc["default_model"] == "kimi-k2.7-code"
    assert doc["models"]["kimi-k2.7-code"]["provider"] == "agedum"
    assert doc["models"]["kimi-k2.7-code"]["max_context_size"] == 262144
    provider = doc["providers"]["agedum"]
    assert provider["type"] == "openai"  # default type (openai_legacy was removed in Kimi Code)
    assert provider["base_url"] == "https://opencode.ai/zen/go/v1"
    assert provider["api_key"] == "sk-go"  # resolved key baked in; masked in --dry-run
    assert doc["thinking"]["enabled"] is True


def test_kimi_code_subscription_uses_kimi_and_subscription_endpoint():
    # `agedum kimi` -> kimi (kimi harness) against the Kimi Code *subscription* endpoint,
    # keyed by KIMI_API_KEY -- never the metered moonshot platform API.
    launch = build_launch(
        {
            "harness": "kimi",
            "secretEnv": "KIMI_API_KEY",
            "config": {
                "binary": "kimi",
                "baseUrl": "https://api.kimi.com/coding/v1",
                "providerType": "kimi",
                "model": "kimi-for-coding",
                "contextWindow": 262144,
                "thinking": True,
                "yolo": True,
            },
        },
        base_env={"KIMI_API_KEY": "sk-kimi-test"},
    )
    assert launch.command == ["kimi", "--model", "kimi-for-coding", "--yolo"]
    target, content, _, _ = launch.config_files[0]
    assert target == str(Path(launch.env["KIMI_CODE_HOME"]) / "config.toml")
    doc = tomllib.loads(content)
    assert doc["default_model"] == "kimi-for-coding"
    assert doc["models"]["kimi-for-coding"]["max_context_size"] == 262144
    provider = doc["providers"]["agedum"]
    assert provider["type"] == "kimi"  # native Kimi Code type, not openai_legacy
    assert provider["base_url"] == "https://api.kimi.com/coding/v1"
    assert "moonshot" not in provider["base_url"]
    assert provider["api_key"] == "sk-kimi-test"


def _kimi_k3_config(**overrides) -> dict:
    """The kimi/kimi-k3-auto launcher shape (mirrors agentsconf kimi/kimi-k3-auto.json)."""
    config = {
        "binary": "kimi",
        "baseUrl": "https://api.kimi.com/coding/v1",
        "providerType": "kimi",
        "model": "k3",
        "contextWindow": 1048576,
        "thinking": True,
        "effortLevel": "max",
        "supportEfforts": ["max"],
        "defaultEffort": "max",
    }
    config.update(overrides)
    return {"harness": "kimi", "secretEnv": "KIMI_API_KEY", "config": config}


def test_kimi_effort_emits_thinking_effort_and_model_support_efforts():
    launch = build_launch(_kimi_k3_config(), base_env=_KIMI_ENV)
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["thinking"]["effort"] == "max"
    assert doc["thinking"]["enabled"] is True
    # support_efforts is what keeps Kimi Code from collapsing the effort to plain `on`.
    assert doc["models"]["k3"]["support_efforts"] == ["max"]
    assert doc["models"]["k3"]["default_effort"] == "max"
    assert doc["models"]["k3"]["max_context_size"] == 1048576


def test_kimi_effort_without_support_efforts_is_rejected():
    # Kimi Code would silently normalise the effort to `on`; agedum refuses the no-op config.
    config = _kimi_k3_config()
    del config["config"]["supportEfforts"]
    with pytest.raises(ProviderError, match="supportEfforts"):
        build_launch(config, base_env=_KIMI_ENV)


def test_kimi_effort_unlisted_in_support_efforts_is_rejected():
    # Kimi Code raises MODEL_CONFIG_INVALID at launch for an effort outside support_efforts.
    with pytest.raises(ProviderError, match="not listed"):
        build_launch(_kimi_k3_config(effortLevel="high"), base_env=_KIMI_ENV)


def test_kimi_effort_widened_support_efforts_allows_high():
    # The seam for later: widen supportEfforts and `high` becomes configurable.
    launch = build_launch(
        _kimi_k3_config(effortLevel="high", supportEfforts=["low", "high", "max"]),
        base_env=_KIMI_ENV,
    )
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["thinking"]["effort"] == "high"
    assert doc["models"]["k3"]["support_efforts"] == ["low", "high", "max"]


def test_kimi_effort_on_openai_provider_type_skips_the_kimi_only_guard():
    # The support_efforts resolution is kimi-wire-protocol only; a compatible endpoint
    # forwards the effort unchanged, so no supportEfforts is required.
    launch = build_launch(
        _kimi_k3_config(providerType="openai", supportEfforts=None, effortLevel="high"),
        base_env=_KIMI_ENV,
    )
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["thinking"]["effort"] == "high"
    assert "support_efforts" not in doc["models"]["k3"]


def _kimi_dual_tier_config(**overrides) -> dict:
    """A two-tier kimi launcher: a wide primary plus a cheaper subagent model."""
    config = {
        "baseUrl": "https://api.kimi.com/coding/v1",
        "providerType": "kimi",
        "model": "k3",
        "subagentModel": "kimi-for-coding",
        "thinking": True,
        "effortLevel": "high",
        "models": {
            "k3": {
                "contextWindow": 1048576,
                "capabilities": ["thinking", "always_thinking"],
                "supportEfforts": ["low", "high", "max"],
                "defaultEffort": "high",
            },
            "kimi-for-coding": {"contextWindow": 262144},
        },
    }
    config.update(overrides)
    return {"harness": "kimi", "secretEnv": "KIMI_API_KEY", "config": config}


def test_kimi_models_map_declares_every_tier():
    launch = build_launch(_kimi_dual_tier_config(), base_env=_KIMI_ENV)
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["default_model"] == "k3"
    assert launch.command == ["kimi", "--model", "k3"]
    assert set(doc["models"]) == {"k3", "kimi-for-coding"}
    assert doc["models"]["k3"]["max_context_size"] == 1048576
    assert doc["models"]["k3"]["support_efforts"] == ["low", "high", "max"]
    # Both tiers ride the one generated provider — a models map is not a second endpoint.
    assert {entry["provider"] for entry in doc["models"].values()} == {"agedum"}
    assert doc["models"]["kimi-for-coding"]["max_context_size"] == 262144
    assert doc["models"]["kimi-for-coding"]["capabilities"] == ["thinking"]  # default
    assert "support_efforts" not in doc["models"]["kimi-for-coding"]


def test_kimi_subagent_model_points_secondary_model_at_the_cheap_tier():
    launch = build_launch(_kimi_dual_tier_config(), base_env=_KIMI_ENV)
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["secondary_model"] == {"model": "kimi-for-coding"}
    assert doc["thinking"]["effort"] == "high"  # applies to the session (primary) model
    # Subagent tiering is an experimental flag, off by default — without this the
    # [secondary_model] section parses and is never consulted.
    assert doc["experimental"] == {"secondary-model": True}


def test_kimi_without_subagent_model_leaves_the_experimental_flag_alone():
    launch = build_launch(_kimi_k3_config(), base_env=_KIMI_ENV)
    doc = tomllib.loads(launch.config_files[0][1])
    assert "experimental" not in doc


def test_kimi_subagent_effort_rides_the_secondary_model_entry():
    launch = build_launch(
        _kimi_dual_tier_config(subagentModel="k3", subagentEffort="low"), base_env=_KIMI_ENV
    )
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["secondary_model"] == {"model": "k3", "default_effort": "low"}


def test_kimi_subagent_model_must_be_declared():
    # Kimi Code fails subagent spawning when [secondary_model].model names no [models] entry.
    with pytest.raises(ProviderError, match="subagentModel"):
        build_launch(_kimi_dual_tier_config(subagentModel="k9"), base_env=_KIMI_ENV)


def test_kimi_subagent_effort_unlisted_for_that_model_is_rejected():
    with pytest.raises(ProviderError, match="subagentEffort"):
        build_launch(
            _kimi_dual_tier_config(subagentModel="kimi-for-coding", subagentEffort="low"),
            base_env=_KIMI_ENV,
        )


def test_kimi_subagent_effort_without_subagent_model_is_rejected():
    config = _kimi_dual_tier_config(subagentEffort="low")
    del config["config"]["subagentModel"]
    with pytest.raises(ProviderError, match="subagentEffort"):
        build_launch(config, base_env=_KIMI_ENV)


def test_kimi_default_model_must_have_a_models_entry():
    with pytest.raises(ProviderError, match="not declared in `models`"):
        build_launch(_kimi_dual_tier_config(model="k9"), base_env=_KIMI_ENV)


def test_kimi_models_map_rejects_top_level_per_model_knobs():
    # A top-level contextWindow applies to no model once `models` is set — reject, don't drop.
    with pytest.raises(ProviderError, match="contextWindow"):
        build_launch(_kimi_dual_tier_config(contextWindow=262144), base_env=_KIMI_ENV)


def test_kimi_effort_checked_against_the_default_model_entry():
    # `high` is listed for k3 (the default) but absent from the subagent tier — still valid,
    # because [thinking] effort applies to the session's model.
    launch = build_launch(_kimi_dual_tier_config(), base_env=_KIMI_ENV)
    doc = tomllib.loads(launch.config_files[0][1])
    assert doc["thinking"]["effort"] == "high"
    # …and an effort the default model does not list is still rejected.
    with pytest.raises(ProviderError, match="not listed"):
        build_launch(_kimi_dual_tier_config(effortLevel="medium"), base_env=_KIMI_ENV)


def test_kimi_single_model_config_is_unchanged_by_the_models_seam():
    # No `models` map: the top-level knobs still describe the one declared model.
    launch = build_launch(_kimi_k3_config(), base_env=_KIMI_ENV)
    doc = tomllib.loads(launch.config_files[0][1])
    assert set(doc["models"]) == {"k3"}
    assert "secondary_model" not in doc


def test_kimi_mcp_servers_generate_mcp_json():
    servers = {
        "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]},
        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
    }
    launch = build_launch(_kimi_k3_config(mcpServers=servers), base_env=_KIMI_ENV)
    targets = {entry[0]: entry for entry in launch.config_files}
    home = Path(launch.env["KIMI_CODE_HOME"])
    mcp_path = str(home / "mcp.json")
    assert mcp_path in targets  # Kimi reads MCP from mcp.json, never config.toml
    target, content, merge_json, writable = targets[mcp_path]
    assert merge_json is False
    assert writable is True
    assert json.loads(content) == {"mcpServers": servers}
    config_toml = targets[str(home / "config.toml")][1]
    assert "mcpServers" not in tomllib.loads(config_toml)


def test_kimi_generated_config_isolates_the_kimi_home():
    # Kimi Code rewrites config.toml by renaming a temp file over it, which EBUSYs against a
    # bind mount — so a generated config moves the whole Kimi home somewhere agedum owns.
    launch = build_launch(_kimi_k3_config(), base_env=_KIMI_ENV)
    home = Path(launch.env["KIMI_CODE_HOME"])
    assert home != Path.home() / ".kimi-code"  # never the user's own Kimi home
    assert home.is_relative_to(Path.home() / ".cache" / "agedum" / "kimi")
    # Same endpoint + model resolves to the same dir, so skills and sessions persist.
    again = build_launch(_kimi_k3_config(), base_env=_KIMI_ENV)
    assert again.env["KIMI_CODE_HOME"] == str(home)
    # A different model is a different launcher, hence a different home.
    other = build_launch(_kimi_k3_config(model="kimi-for-coding"), base_env=_KIMI_ENV)
    assert other.env["KIMI_CODE_HOME"] != str(home)


def test_kimi_without_a_generated_config_keeps_the_real_home():
    # No baseUrl: nothing is generated, Kimi runs on its own account config, and an mcp.json
    # is still injected — read-only bound at the real home, as before.
    servers = {"context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]}}
    launch = build_launch({"harness": "kimi", "config": {"mcpServers": servers}}, base_env={})
    assert "KIMI_CODE_HOME" not in launch.env
    assert len(launch.config_files) == 1
    entry = launch.config_files[0]
    assert entry[0] == str(Path.home() / ".kimi-code" / "mcp.json")
    assert json.loads(entry[1]) == {"mcpServers": servers}
    assert len(entry) == 3  # read-only bind, not a writable seed


def test_kimi_mcp_servers_need_an_object():
    with pytest.raises(ProviderError, match="mcpServers"):
        build_launch(_kimi_k3_config(mcpServers=["context7"]), base_env=_KIMI_ENV)


def test_kimi_mcp_servers_reject_an_env_placeholder():
    # Kimi Code is not known to expand ${VAR} in mcp.json, so a shared MCP base extended by
    # a kimi launcher must fail loudly instead of handing the server a literal placeholder.
    servers = {"nodum": {"command": "nodum", "env": {"TOKEN": "${NODUM_AGENT_TOKEN}"}}}
    with pytest.raises(ProviderError, match="placeholder"):
        build_launch(_kimi_k3_config(mcpServers=servers), base_env=_KIMI_ENV)


def test_kimi_mcp_servers_without_base_url_still_inject():
    # MCP is independent of the endpoint, so it must not be gated on the config.toml path.
    launch = build_launch(
        {
            "harness": "kimi",
            "config": {"mcpServers": {"context7": {"command": "npx", "args": ["-y", "x"]}}},
        },
        base_env={},
    )
    assert [entry[0] for entry in launch.config_files] == [
        str(Path.home() / ".kimi-code" / "mcp.json")
    ]


def test_kimi_binary_override_and_default():
    # `binary` overrides the CLI name; default is `kimi`.
    overridden = build_launch(
        {"harness": "kimi", "config": {"binary": "kimi-cli", "model": "kimi-for-coding"}},
        base_env={},
    )
    assert overridden.command == ["kimi-cli", "--model", "kimi-for-coding"]
    default = build_launch({"harness": "kimi", "config": {"model": "m"}}, base_env={})
    assert default.command[0] == "kimi"


def test_kimi_base_url_requires_model():
    with pytest.raises(ProviderError, match="no `model`"):
        build_launch(
            {
                "harness": "kimi",
                "secretEnv": "OPENCODE_GO_API_KEY",
                "config": {"baseUrl": "https://opencode.ai/zen/go/v1"},
            },
            base_env={"OPENCODE_GO_API_KEY": "sk-go"},
        )


def test_kimi_base_url_requires_secret_env():
    with pytest.raises(ProviderError, match="secretEnv"):
        build_launch(
            {
                "harness": "kimi",
                "config": {
                    "baseUrl": "https://opencode.ai/zen/go/v1",
                    "model": "kimi-k2.7-code",
                },
            },
            base_env={},
        )


# --- opencode env mapping ---


def test_opencode_agent_options():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {
                "model": "deepseek/deepseek-v4-flash",
                "disableExternalSkills": True,
                "agentOptions": [
                    {"agent": "build", "model": "deepseek/deepseek-v4-pro"},
                    {
                        "agent": "high",
                        "model": "deepseek/deepseek-v4-pro",
                        "primary": True,
                        "reasoningEffort": "high",
                    },
                ],
            },
        },
        base_env={},
    )
    assert launch.env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["model"] == "deepseek/deepseek-v4-flash"
    assert payload["agent"]["build"]["model"] == "deepseek/deepseek-v4-pro"
    assert payload["agent"]["high"]["mode"] == "primary"
    assert "mode" not in payload["agent"]["build"]
    assert payload["agent"]["high"]["options"]["reasoningEffort"] == "high"
    assert launch.command == ["opencode"]


def test_opencode_flat_effort_alias():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {"model": "deepseek/deepseek-v4-flash", "effortLevel": "low"},
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    options = payload["provider"]["deepseek"]["models"]["deepseek-v4-flash"]["options"]
    assert options["reasoningEffort"] == "low"


def test_opencode_options_without_addressable_model_fail_loudly():
    # effortLevel/defaultOptions attach under provider.<id>.models.<id>; without a
    # provider/model-shaped `model` they would silently do nothing — must raise instead.
    for model_value in ("", "deepseek-v4-flash"):
        with pytest.raises(ProviderError, match="provider/model"):
            build_launch(
                {
                    "harness": "opencode",
                    "config": {"model": model_value, "effortLevel": "low"},
                },
                base_env={},
            )


def test_opencode_explicit_options_win_over_flat_effort():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {
                "model": "p/m",
                "effortLevel": "low",
                "defaultOptions": {"reasoningEffort": "high"},
            },
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["provider"]["p"]["models"]["m"]["options"]["reasoningEffort"] == "high"


def test_opencode_config_passthrough_object_merges():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {"model": "p/m", "opencodeConfig": {"theme": "tokyonight"}},
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["theme"] == "tokyonight"
    assert payload["model"] == "p/m"


def test_opencode_config_passthrough_wins_over_modeled_keys():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {"model": "p/m", "opencodeConfig": {"model": "x/y"}},
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert payload["model"] == "x/y"


def test_opencode_auto_injects_transcript_plugin():
    # Even a config-less opencode provider gets the bundled transcript plugin,
    # so condash (and any pty capturer) gets a clean transcript for free.
    launch = build_launch({"harness": "opencode", "config": {}}, base_env={})
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert any(p.endswith("assets/opencode/transcript-osc.js") for p in payload["plugin"])


def test_opencode_transcript_plugin_opt_out():
    launch = build_launch(
        {"harness": "opencode", "config": {"model": "p/m", "emitTranscript": False}},
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert "plugin" not in payload


def test_opencode_transcript_plugin_unions_with_passthrough_plugins():
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {"opencodeConfig": {"plugin": ["my-other-plugin"]}},
        },
        base_env={},
    )
    payload = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert "my-other-plugin" in payload["plugin"]
    assert any(p.endswith("assets/opencode/transcript-osc.js") for p in payload["plugin"])


def test_opencode_config_passthrough_rejects_non_object():
    with pytest.raises(ProviderError):
        build_launch(
            {"harness": "opencode", "config": {"opencodeConfig": "nope"}},
            base_env={},
        )


def test_opencode_permission_key_order_is_preserved():
    # opencode evaluates a permission map in key insertion order and keeps the LAST
    # matching rule, so a trailing guard is what bounds a permissive prefix glob.
    # Serializing the doc sorted moved every `*…` guard ahead of the alphabetic allows
    # and inverted the outcome: `git log … | sh` matched `git log*` last and was allowed.
    authored = {
        "*": "deny",
        "git log*": "allow",
        "condash projects list*": "allow",
        "*|*": "deny",
        "*;*": "deny",
    }
    launch = build_launch(
        {
            "harness": "opencode",
            "config": {
                "opencodeConfig": {"agent": {"explorer": {"permission": {"bash": authored}}}}
            },
        },
        base_env={},
    )
    emitted = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    keys = list(emitted["agent"]["explorer"]["permission"]["bash"])
    assert keys == list(authored)
    # The guards must land after the allow they exist to override, or they never win.
    assert keys.index("*|*") > keys.index("git log*")
    assert keys != sorted(authored)


def _opencode_agents(config):
    """Build an opencode launch and return its config doc's `agent` block."""
    launch = build_launch({"harness": "opencode", "config": config}, base_env={})
    return json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])["agent"]


def test_opencode_agent_append_folds_into_prompt():
    # agentAppend text is appended after the agent's prompt, and the synthetic key is
    # stripped so opencode never sees it.
    agents = _opencode_agents(
        {
            "opencodeConfig": {
                "agent": {
                    "conception": {
                        "mode": "primary",
                        "prompt": "You are the planning agent.",
                        "agentAppend": "## Handoff rule\n\nHand off to the build agent.",
                    }
                }
            }
        }
    )
    assert agents["conception"]["prompt"] == (
        "You are the planning agent.\n\n## Handoff rule\n\nHand off to the build agent."
    )
    assert "agentAppend" not in agents["conception"]
    assert agents["conception"]["mode"] == "primary"


def test_opencode_agent_append_list_concatenates():
    agents = _opencode_agents(
        {
            "opencodeConfig": {
                "agent": {
                    "conception": {
                        "prompt": "Base prompt.",
                        "agentAppend": ["## Rule A\n\nfirst", "## Rule B\n\nsecond"],
                    }
                }
            }
        }
    )
    assert agents["conception"]["prompt"] == (
        "Base prompt.\n\n## Rule A\n\nfirst\n\n## Rule B\n\nsecond"
    )


def test_opencode_agent_append_null_is_noop():
    # A null agentAppend (an extends child clearing an inherited append) leaves the prompt
    # untouched and still strips the key.
    agents = _opencode_agents(
        {
            "opencodeConfig": {
                "agent": {"build": {"prompt": "You are the build agent.", "agentAppend": None}}
            }
        }
    )
    assert agents["build"]["prompt"] == "You are the build agent."
    assert "agentAppend" not in agents["build"]


def test_opencode_agent_append_without_prompt_becomes_the_prompt():
    agents = _opencode_agents(
        {"opencodeConfig": {"agent": {"conception": {"agentAppend": "## Rule\n\nbody"}}}}
    )
    assert agents["conception"]["prompt"] == "## Rule\n\nbody"


def test_opencode_agent_append_only_affects_agents_that_declare_it():
    agents = _opencode_agents(
        {
            "opencodeConfig": {
                "agent": {
                    "conception": {"prompt": "plan", "agentAppend": "extra"},
                    "build": {"prompt": "build"},
                }
            }
        }
    )
    assert agents["conception"]["prompt"] == "plan\n\nextra"
    assert agents["build"]["prompt"] == "build"


def test_opencode_agent_append_extends_inheritance(tmp_path):
    # A base defines agent + agentAppend; the child inherits it through extends. A second
    # child clears it with null. Both resolve via load_merged_config before build_launch.
    _write_config(
        tmp_path,
        "base/plan.json",
        {
            "abstract": True,
            "harness": "opencode",
            "config": {
                "opencodeConfig": {
                    "agent": {"conception": {"prompt": "Plan.", "agentAppend": "Handoff rule."}}
                }
            },
        },
    )
    inherit = load_merged_config(
        _write_config(tmp_path, "inherit.json", {"extends": "base/plan"}), tmp_path
    )
    agents = _opencode_agents(inherit["config"])
    assert agents["conception"]["prompt"] == "Plan.\n\nHandoff rule."

    cleared = load_merged_config(
        _write_config(
            tmp_path,
            "cleared.json",
            {
                "extends": "base/plan",
                "config": {"opencodeConfig": {"agent": {"conception": {"agentAppend": None}}}},
            },
        ),
        tmp_path,
    )
    cleared_agents = _opencode_agents(cleared["config"])
    assert cleared_agents["conception"]["prompt"] == "Plan."
    assert "agentAppend" not in cleared_agents["conception"]


def test_opencode_agent_append_rejects_invalid_type():
    with pytest.raises(ProviderError, match="agentAppend"):
        build_launch(
            {
                "harness": "opencode",
                "config": {"opencodeConfig": {"agent": {"c": {"agentAppend": 5}}}},
            },
            base_env={},
        )


def test_opencode_agent_append_rejects_non_string_list_entry():
    with pytest.raises(ProviderError, match="agentAppend"):
        build_launch(
            {
                "harness": "opencode",
                "config": {"opencodeConfig": {"agent": {"c": {"agentAppend": ["ok", 3]}}}},
            },
            base_env={},
        )


def test_opencode_agent_append_does_not_mutate_input_config():
    # build_launch must not edit the caller's config: the fold copies the entries it touches
    # rather than popping/rewriting the aliased passthrough dicts in place.
    config = {
        "harness": "opencode",
        "config": {
            "opencodeConfig": {"agent": {"conception": {"prompt": "plan", "agentAppend": "extra"}}}
        },
    }
    launch = build_launch(config, base_env={})
    assert config["config"]["opencodeConfig"]["agent"]["conception"] == {
        "prompt": "plan",
        "agentAppend": "extra",
    }
    # ...while the generated doc still carries the folded prompt with the key stripped.
    folded = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])["agent"]["conception"]
    assert folded == {"prompt": "plan\n\nextra"}


def test_opencode_agent_append_rejects_non_string_prompt():
    # A non-string prompt beside agentAppend is a malformed agent config — raise rather than
    # coerce it to its Python repr.
    with pytest.raises(ProviderError, match="prompt"):
        build_launch(
            {
                "harness": "opencode",
                "config": {
                    "opencodeConfig": {"agent": {"c": {"prompt": {"x": 1}, "agentAppend": "y"}}}
                },
            },
            base_env={},
        )


def test_build_launch_is_deterministic():
    config = {
        "harness": "opencode",
        "secretEnv": "K",
        "config": {
            "model": "p/m",
            "agentOptions": [
                {"agent": "high", "reasoningEffort": "high", "textVerbosity": "medium"},
                {"agent": "plan", "reasoningEffort": "low"},
            ],
        },
    }
    a = build_launch(config, base_env={"K": "v"})
    b = build_launch(json.loads(json.dumps(config)), base_env={"K": "v"})
    assert a == b
    assert isinstance(a, Launch)


def _provider_def_config():
    return {
        "harness": "opencode",
        "requiredEnv": ["OPENROUTER_API_KEY"],
        "config": {
            "model": "openrouter/moonshotai/kimi-k2.6",
            "effortLevel": "max",
            "providerDef": {
                "id": "openrouter",
                "npm": "@openrouter/ai-sdk-provider",
                "baseUrl": "https://openrouter.ai/api/v1",
                "apiKeyEnv": "OPENROUTER_API_KEY",
            },
        },
    }


def test_opencode_provider_def_renders_explicit_provider_with_key():
    launch = build_launch(_provider_def_config(), base_env={"OPENROUTER_API_KEY": "sk-or-v1-xyz"})
    provider = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])["provider"]["openrouter"]
    assert provider["npm"] == "@openrouter/ai-sdk-provider"
    assert provider["options"]["baseURL"] == "https://openrouter.ai/api/v1"
    # The resolved key value is baked straight into the config doc (no {env:} placeholder).
    assert provider["options"]["apiKey"] == "sk-or-v1-xyz"
    # Deep-merged with the per-model reasoning options, not clobbered.
    assert provider["models"]["moonshotai/kimi-k2.6"]["options"]["reasoningEffort"] == "max"


def test_opencode_provider_def_masks_config_doc_in_dry_run():
    launch = build_launch(_provider_def_config(), base_env={"OPENROUTER_API_KEY": "sk-or-v1-xyz"})
    # The doc embeds the key, so the whole var is masked in --dry-run.
    assert "OPENCODE_CONFIG_CONTENT" in launch.secrets


def test_opencode_provider_def_key_env_validated_even_if_unlisted():
    config = _provider_def_config()
    config.pop("requiredEnv")  # apiKeyEnv must still be required defensively
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY is required"):
        build_launch(config, base_env={})


def test_opencode_provider_def_missing_field_errors():
    config = _provider_def_config()
    del config["config"]["providerDef"]["npm"]
    with pytest.raises(ProviderError, match="providerDef is missing required field"):
        build_launch(config, base_env={"OPENROUTER_API_KEY": "sk-or-v1-xyz"})


def _provider_def_list_config():
    # One config drawing a Kimi primary + DeepSeek fast subagents, each provider keyed.
    return {
        "harness": "opencode",
        "requiredEnv": ["KIMI_API_KEY", "DEEPSEEK_API_KEY"],
        "config": {
            "model": "kimi-for-coding/kimi-k2.6",
            "agentOptions": [
                {"agent": "general", "model": "deepseek/deepseek-v4-flash"},
            ],
            "providerDef": [
                {
                    "id": "kimi-for-coding",
                    "npm": "@ai-sdk/anthropic",
                    "baseUrl": "https://api.kimi.com/coding/v1",
                    "apiKeyEnv": "KIMI_API_KEY",
                },
                {
                    "id": "deepseek",
                    "npm": "@ai-sdk/openai-compatible",
                    "baseUrl": "https://api.deepseek.com",
                    "apiKeyEnv": "DEEPSEEK_API_KEY",
                },
            ],
        },
    }


def test_opencode_provider_def_list_renders_every_provider_with_its_key():
    launch = build_launch(
        _provider_def_list_config(),
        base_env={"KIMI_API_KEY": "sk-kimi-abc", "DEEPSEEK_API_KEY": "sk-ds-xyz"},
    )
    providers = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])["provider"]
    assert providers["kimi-for-coding"]["options"]["apiKey"] == "sk-kimi-abc"
    assert providers["kimi-for-coding"]["options"]["baseURL"] == "https://api.kimi.com/coding/v1"
    assert providers["deepseek"]["options"]["apiKey"] == "sk-ds-xyz"
    assert providers["deepseek"]["npm"] == "@ai-sdk/openai-compatible"


def test_opencode_provider_def_list_validates_every_key_env():
    config = _provider_def_list_config()
    config.pop("requiredEnv")  # every entry's apiKeyEnv must still be required defensively
    with pytest.raises(ProviderError, match="DEEPSEEK_API_KEY is required"):
        build_launch(config, base_env={"KIMI_API_KEY": "sk-kimi-abc"})


def test_opencode_provider_def_list_rejects_non_dict_entry():
    config = _provider_def_list_config()
    config["config"]["providerDef"].append("nope")
    with pytest.raises(ProviderError, match="each `providerDef` entry must be a JSON object"):
        build_launch(
            config,
            base_env={"KIMI_API_KEY": "sk-kimi-abc", "DEEPSEEK_API_KEY": "sk-ds-xyz"},
        )


# --- cline env/command mapping ---


def test_cline_appends_flags_and_passes_key():
    launch = build_launch(
        {
            "harness": "cline",
            "secretEnv": "ANTHROPIC_API_KEY",
            "config": {
                "model": "claude-opus-4-8",
                "provider": "anthropic",
                "effortLevel": "high",
                "plan": True,
            },
        },
        base_env={"ANTHROPIC_API_KEY": "sk-cline-abc"},
    )
    # Cline takes provider/model/effort as flags and the token as `--key` (in argv).
    assert launch.command == [
        "cline",
        "--model",
        "claude-opus-4-8",
        "--provider",
        "anthropic",
        "--thinking",
        "high",
        "--plan",
        "--key",
        "sk-cline-abc",
    ]
    # The token still rides the required-env export, so it is masked in --dry-run.
    assert launch.env["ANTHROPIC_API_KEY"] == "sk-cline-abc"
    assert "ANTHROPIC_API_KEY" in launch.secrets


def test_cline_bare_runs_plain():
    launch = build_launch({"harness": "cline", "config": {}}, base_env={})
    assert launch.command == ["cline"]
    assert launch.env == {}


def test_cline_no_key_flag_without_secret_env():
    # A provider that relies on Cline's own pre-configured auth (`cline auth`) carries no
    # secretEnv, so no `--key` is appended.
    launch = build_launch(
        {"harness": "cline", "config": {"provider": "cline", "model": "x"}}, base_env={}
    )
    assert launch.command == ["cline", "--model", "x", "--provider", "cline"]
    assert "--key" not in launch.command


def test_cline_auto_approve_flag():
    # autoApprove maps to cline's --auto-approve <boolean>; absent leaves cline's own default.
    on = build_launch(
        {"harness": "cline", "config": {"provider": "deepseek", "autoApprove": True}},
        base_env={},
    )
    assert on.command == ["cline", "--provider", "deepseek", "--auto-approve", "true"]
    off = build_launch(
        {"harness": "cline", "config": {"provider": "deepseek", "autoApprove": False}},
        base_env={},
    )
    assert off.command == ["cline", "--provider", "deepseek", "--auto-approve", "false"]
    bare = build_launch({"harness": "cline", "config": {"provider": "deepseek"}}, base_env={})
    assert "--auto-approve" not in bare.command


def test_cline_base_url_generates_isolated_providers_config():
    # A custom OpenAI-compatible endpoint: cline honours a custom base URL only from a stored
    # provider, so agedum writes a single-provider providers.json under an isolated
    # CLINE_DATA_DIR and launches with no --provider/--model (which would rebuild the provider
    # and drop the base URL). The key rides --key and is masked; nothing secret is on disk.
    launch = build_launch(
        {
            "harness": "cline",
            "secretEnv": "KIMI_API_KEY",
            "config": {
                "baseUrl": "https://api.kimi.com/coding/v1",
                "model": "kimi-for-coding",
                "autoApprove": True,
            },
        },
        base_env={"KIMI_API_KEY": "sk-kimi-xyz"},
    )
    slug = "https-api-kimi-com-coding-v1-kimi-for-coding"
    data_dir = str(Path.home() / ".cache" / "agedum" / "cline" / slug)
    assert launch.env["CLINE_DATA_DIR"] == data_dir
    # No --provider/--model on the command; the key is the only secret and rides --key.
    assert launch.command == ["cline", "--auto-approve", "true", "--key", "sk-kimi-xyz"]
    assert "--provider" not in launch.command and "--model" not in launch.command
    assert "KIMI_API_KEY" in launch.secrets

    # One generated config file: the single-provider providers.json (no key baked in).
    # It is a *writable* seed (4th field True) so cline can rewrite it without EROFS.
    assert len(launch.config_files) == 1
    target, content, merge_json, writable = launch.config_files[0]
    assert target == f"{data_dir}/settings/providers.json"
    assert merge_json is False
    assert writable is True
    doc = json.loads(content)
    assert doc["lastUsedProvider"] == "openai-compatible"
    settings = doc["providers"]["openai-compatible"]["settings"]
    assert settings["baseUrl"] == "https://api.kimi.com/coding/v1"
    assert settings["model"] == "kimi-for-coding"
    assert settings["apiKey"] == ""  # key is never written to disk
    assert "sk-kimi-xyz" not in content


def test_cline_base_url_requires_model():
    with pytest.raises(ProviderError, match="baseUrl` but no `model`"):
        build_launch(
            {
                "harness": "cline",
                "secretEnv": "KIMI_API_KEY",
                "config": {"baseUrl": "https://api.kimi.com/coding/v1"},
            },
            base_env={"KIMI_API_KEY": "tok"},
        )


def test_cline_base_url_rejects_named_provider():
    # A custom endpoint goes through the generated openai-compatible provider, so pairing
    # baseUrl with a named `provider` is a config mistake, not a silent override.
    with pytest.raises(ProviderError, match="both `baseUrl` and `provider`"):
        build_launch(
            {
                "harness": "cline",
                "secretEnv": "KIMI_API_KEY",
                "config": {
                    "baseUrl": "https://api.kimi.com/coding/v1",
                    "model": "kimi-for-coding",
                    "provider": "openai-compatible",
                },
            },
            base_env={"KIMI_API_KEY": "tok"},
        )


def test_cline_compaction_flag():
    # compaction → --compaction <mode>, on both the named-provider and baseUrl paths.
    named = build_launch(
        {"harness": "cline", "config": {"provider": "opencode", "compaction": "agentic"}},
        base_env={},
    )
    assert named.command == ["cline", "--provider", "opencode", "--compaction", "agentic"]
    with pytest.raises(ProviderError, match="compaction` must be agentic"):
        build_launch(
            {"harness": "cline", "config": {"provider": "x", "compaction": "smart"}}, base_env={}
        )


def test_cline_base_url_context_window_becomes_models_array():
    # contextWindow / maxTokens teach cline's catalogue-less openai-compatible provider the
    # model's window (compaction threshold + X/N meter) and output cap via a one-entry models[].
    launch = build_launch(
        {
            "harness": "cline",
            "secretEnv": "KIMI_API_KEY",
            "config": {
                "baseUrl": "https://api.kimi.com/coding/v1",
                "model": "kimi-for-coding",
                "contextWindow": 262144,
                "maxTokens": 32768,
                "compaction": "agentic",
            },
        },
        base_env={"KIMI_API_KEY": "sk-kimi-xyz"},
    )
    assert "--compaction" in launch.command and "agentic" in launch.command
    settings = json.loads(launch.config_files[0][1])["providers"]["openai-compatible"]["settings"]
    assert settings["models"] == [
        {"id": "kimi-for-coding", "contextWindow": 262144, "maxTokens": 32768}
    ]
    # No window fields → no models array (cline keeps its default window).
    bare = build_launch(
        {
            "harness": "cline",
            "secretEnv": "KIMI_API_KEY",
            "config": {"baseUrl": "https://api.kimi.com/coding/v1", "model": "kimi-for-coding"},
        },
        base_env={"KIMI_API_KEY": "tok"},
    )
    bare_doc = json.loads(bare.config_files[0][1])
    assert "models" not in bare_doc["providers"]["openai-compatible"]["settings"]


def test_cline_context_window_rejects_junk():
    with pytest.raises(ProviderError, match="`contextWindow` must be a positive integer"):
        build_launch(
            {
                "harness": "cline",
                "secretEnv": "KIMI_API_KEY",
                "config": {
                    "baseUrl": "https://api.kimi.com/coding/v1",
                    "model": "kimi-for-coding",
                    "contextWindow": -5,
                },
            },
            base_env={"KIMI_API_KEY": "tok"},
        )


# --- reasonix env/command mapping ---


def test_reasonix_chat_subcommand_and_model_flag():
    launch = build_launch(
        {
            "harness": "reasonix",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"model": "deepseek-pro"},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-rx-abc"},
    )
    # `chat` is the interactive subcommand; --model selects a reasonix provider by name.
    assert launch.command == ["reasonix", "chat", "--model", "deepseek-pro"]
    # No baseUrl → no generated config file (uses reasonix's built-in/configured providers).
    assert launch.config_files == ()
    # The token rides the required-env export (reasonix reads it via api_key_env) and is masked.
    assert launch.env["DEEPSEEK_API_KEY"] == "sk-rx-abc"
    assert "DEEPSEEK_API_KEY" in launch.secrets


def test_reasonix_bare_runs_chat():
    # No model: bare `reasonix chat` (uses the config default_model). No env beyond the key.
    launch = build_launch({"harness": "reasonix", "config": {}}, base_env={})
    assert launch.command == ["reasonix", "chat"]
    assert launch.env == {}
    assert launch.config_files == ()


def test_reasonix_custom_endpoint_generates_toml():
    # A baseUrl makes agedum generate a ./reasonix.toml [[providers]] block + default_model
    # and select it by the fixed agedum provider name; `model` is the upstream model id.
    launch = build_launch(
        {
            "harness": "reasonix",
            "slug": "reasonix-myhost",
            "secretEnv": "MY_API_KEY",
            "config": {"baseUrl": "https://my.host/v1", "model": "deepseek-v4-pro"},
        },
        base_env={"MY_API_KEY": "sk-secret-xyz"},
    )
    assert launch.command == ["reasonix", "chat", "--model", "agedum"]
    assert [entry[0] for entry in launch.config_files] == ["reasonix.toml"]
    assert launch.config_files[0][2] is False  # reasonix.toml is written verbatim, not merged
    toml = launch.config_files[0][1]
    assert 'default_model = "agedum"' in toml
    assert "[[providers]]" in toml
    assert 'name = "agedum"' in toml
    assert 'kind = "openai"' in toml
    assert 'base_url = "https://my.host/v1"' in toml
    assert 'model = "deepseek-v4-pro"' in toml
    assert 'api_key_env = "MY_API_KEY"' in toml
    # The toml references the key by env-var NAME — never its value (no secret on disk).
    assert "sk-secret-xyz" not in toml
    # The token still rides the required-env export so reasonix resolves api_key_env.
    assert launch.env["MY_API_KEY"] == "sk-secret-xyz"
    assert "MY_API_KEY" in launch.secrets


def test_reasonix_toml_escapes_control_characters():
    # A value carrying a newline / tab must not emit invalid TOML — basic strings may not
    # contain raw control characters, so they are escaped.
    launch = build_launch(
        {
            "harness": "reasonix",
            "config": {"baseUrl": "https://h/v1", "model": 'we"ird\nmo\tdel'},
        },
        base_env={},
    )
    toml = launch.config_files[0][1]
    assert '\\"' in toml  # quote escaped
    assert "\\n" in toml and "\\t" in toml  # control chars escaped
    assert 'model = "we\\"ird\\nmo\\tdel"' in toml


def test_reasonix_custom_endpoint_kind_override():
    launch = build_launch(
        {
            "harness": "reasonix",
            "config": {"baseUrl": "https://h/v1", "model": "m", "kind": "anthropic"},
        },
        base_env={},
    )
    assert 'kind = "anthropic"' in launch.config_files[0][1]


def test_reasonix_keyless_endpoint_omits_api_key_env():
    # No secretEnv (a local keyless endpoint): no api_key_env line, and no required env.
    launch = build_launch(
        {"harness": "reasonix", "config": {"baseUrl": "http://localhost:1234/v1", "model": "m"}},
        base_env={},
    )
    assert "api_key_env" not in launch.config_files[0][1]
    assert launch.command == ["reasonix", "chat", "--model", "agedum"]


def test_reasonix_base_url_requires_model():
    # Generating a toml needs an executor: baseUrl without model is a fail-loud error.
    with pytest.raises(ProviderError, match="reasonix needs `model`"):
        build_launch(
            {"harness": "reasonix", "config": {"baseUrl": "https://my.host/v1"}}, base_env={}
        )


# --- reasonix two-model routing (subagent / planner) + multi-provider ---


def test_reasonix_subagent_model_builtin_tier():
    # subagentModel with built-in executor/subagent: a reasonix.toml with default_model +
    # [agent] subagent_model and NO [[providers]] (so reasonix's built-ins survive the merge).
    launch = build_launch(
        {
            "harness": "reasonix",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"model": "deepseek-pro", "subagentModel": "deepseek-flash"},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert launch.command == ["reasonix", "chat", "--model", "deepseek-pro"]
    toml = launch.config_files[0][1]
    assert 'default_model = "deepseek-pro"' in toml
    assert "[agent]" in toml
    assert 'subagent_model = "deepseek-flash"' in toml
    assert "[[providers]]" not in toml  # both are built-ins; no provider block needed


def test_reasonix_provider_def_list_two_providers():
    # providerDef list (kimi executor + deepseek-flash subagents): two [[providers]] blocks +
    # [agent] subagent_model; both keys are auto-added to requiredEnv and exported (masked).
    config = {
        "harness": "reasonix",
        "slug": "reasonix-kimi-flash",
        "config": {
            "model": "kimi",
            "subagentModel": "deepseek-flash",
            "providerDef": [
                {
                    "id": "kimi",
                    "kind": "anthropic",
                    "baseUrl": "https://api.kimi.com/coding",
                    "model": "k2p6",
                    "apiKeyEnv": "KIMI_API_KEY",
                },
                {
                    "id": "deepseek-flash",
                    "kind": "openai",
                    "baseUrl": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash",
                    "apiKeyEnv": "DEEPSEEK_API_KEY",
                },
            ],
        },
    }
    assert required_env(config) == ["KIMI_API_KEY", "DEEPSEEK_API_KEY"]
    launch = build_launch(config, base_env={"KIMI_API_KEY": "sk-kimi", "DEEPSEEK_API_KEY": "sk-ds"})
    assert launch.command == ["reasonix", "chat", "--model", "kimi"]
    toml = launch.config_files[0][1]
    assert 'default_model = "kimi"' in toml
    assert 'subagent_model = "deepseek-flash"' in toml
    assert toml.count("[[providers]]") == 2
    assert 'name = "kimi"' in toml and 'kind = "anthropic"' in toml
    assert 'base_url = "https://api.kimi.com/coding"' in toml
    assert 'api_key_env = "KIMI_API_KEY"' in toml and 'api_key_env = "DEEPSEEK_API_KEY"' in toml
    # Keys are referenced by name, never value.
    assert "sk-kimi" not in toml and "sk-ds" not in toml
    assert {"KIMI_API_KEY", "DEEPSEEK_API_KEY"} <= launch.secrets


def test_reasonix_planner_and_auto_plan():
    launch = build_launch(
        {
            "harness": "reasonix",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"model": "deepseek-pro", "plannerModel": "mimo-pro", "autoPlan": "on"},
        },
        base_env={"DEEPSEEK_API_KEY": "t"},
    )
    toml = launch.config_files[0][1]
    assert 'planner_model = "mimo-pro"' in toml
    assert 'auto_plan = "on"' in toml


def test_reasonix_auto_plan_rejects_unknown_value():
    with pytest.raises(ProviderError, match="autoPlan` must be one of"):
        build_launch(
            {"harness": "reasonix", "config": {"model": "x", "autoPlan": "sometimes"}}, base_env={}
        )


def test_reasonix_base_url_and_provider_def_are_mutually_exclusive():
    with pytest.raises(ProviderError, match="both `baseUrl` and `providerDef`"):
        build_launch(
            {
                "harness": "reasonix",
                "config": {
                    "model": "x",
                    "baseUrl": "https://h/v1",
                    "providerDef": {"id": "p", "baseUrl": "https://h2/v1", "model": "m"},
                },
            },
            base_env={},
        )


def test_reasonix_provider_def_missing_field_fails_loudly():
    with pytest.raises(ProviderError, match="providerDef is missing required field"):
        build_launch(
            {
                "harness": "reasonix",
                "config": {"model": "p", "providerDef": {"id": "p", "baseUrl": "https://h/v1"}},
            },
            base_env={},
        )


# --- aider env/command mapping ---


def test_aider_git_disabled_by_default():
    # The headline default: agedum's namespace shares the real .git, so aider's git
    # integration is disabled unless the config opts in.
    launch = build_launch(
        {
            "harness": "aider",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"model": "deepseek/deepseek-chat"},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-aider-abc"},
    )
    assert launch.command == ["aider", "--model", "deepseek/deepseek-chat", "--no-git"]
    assert launch.config_files == ()
    # The key rides the required-env export (litellm reads it by name) and is masked.
    assert launch.env["DEEPSEEK_API_KEY"] == "sk-aider-abc"
    assert "DEEPSEEK_API_KEY" in launch.secrets


def test_aider_git_disabled_explicitly():
    # `git: false` is the same as omitting it — both disable git integration.
    launch = build_launch({"harness": "aider", "config": {"model": "m", "git": False}}, base_env={})
    assert launch.command == ["aider", "--model", "m", "--no-git"]


def test_aider_git_enabled_omits_no_git():
    # `git: true` opts back into aider's git integration (no --no-git appended).
    launch = build_launch({"harness": "aider", "config": {"model": "m", "git": True}}, base_env={})
    assert launch.command == ["aider", "--model", "m"]
    assert "--no-git" not in launch.command


def test_aider_git_enabled_with_auto_commits_off():
    # With git on, `autoCommits: false` still suppresses commits via --no-auto-commits.
    launch = build_launch(
        {"harness": "aider", "config": {"model": "m", "git": True, "autoCommits": False}},
        base_env={},
    )
    assert launch.command == ["aider", "--model", "m", "--no-auto-commits"]


def test_aider_full_model_mapping():
    launch = build_launch(
        {
            "harness": "aider",
            "config": {
                "model": "openai/gpt-x",
                "weakModel": "openai/gpt-mini",
                "editorModel": "openai/gpt-edit",
                "reasoningEffort": "high",
                "yesAlways": True,
            },
        },
        base_env={},
    )
    assert launch.command == [
        "aider",
        "--model",
        "openai/gpt-x",
        "--weak-model",
        "openai/gpt-mini",
        "--editor-model",
        "openai/gpt-edit",
        "--reasoning-effort",
        "high",
        "--no-git",
        "--yes-always",
    ]


def test_aider_base_url_sets_openai_api_base():
    # A custom OpenAI-compatible endpoint -> OPENAI_API_BASE (litellm reads it by name).
    launch = build_launch(
        {
            "harness": "aider",
            "secretEnv": "OPENAI_API_KEY",
            "config": {"model": "openai/local", "baseUrl": "https://my.host/v1"},
        },
        base_env={"OPENAI_API_KEY": "sk-x"},
    )
    assert launch.env["OPENAI_API_BASE"] == "https://my.host/v1"
    assert launch.command == ["aider", "--model", "openai/local", "--no-git"]


def test_aider_bare_runs_with_no_git_only():
    # No model: bare `aider`, still git-disabled by default.
    launch = build_launch({"harness": "aider", "config": {}}, base_env={})
    assert launch.command == ["aider", "--no-git"]
    assert launch.config_files == ()


# --- pi (earendil-works pi-coding-agent) ---


def test_pi_basic_model_provider_thinking():
    # model/provider/thinking are plain CLI flags; no on-disk config without baseUrl.
    launch = build_launch(
        {
            "harness": "pi",
            "secretEnv": "ANTHROPIC_API_KEY",
            "config": {
                "model": "anthropic/claude-sonnet-4",
                "provider": "anthropic",
                "thinking": "high",
            },
        },
        base_env={"ANTHROPIC_API_KEY": "sk-x"},
    )
    assert launch.command == [
        "pi",
        "--model",
        "anthropic/claude-sonnet-4",
        "--provider",
        "anthropic",
        "--thinking",
        "high",
    ]
    assert launch.config_files == ()


def test_pi_bare_runs_pi():
    launch = build_launch({"harness": "pi", "config": {}}, base_env={})
    assert launch.command == ["pi"]
    assert launch.config_files == ()


def test_pi_key_via_env_export_not_argv():
    # The key reaches pi via the required-env export (its conventional name), never argv.
    launch = build_launch(
        {"harness": "pi", "secretEnv": "DEEPSEEK_API_KEY", "config": {"model": "deepseek-chat"}},
        base_env={"DEEPSEEK_API_KEY": "sk-secret"},
    )
    assert launch.env["DEEPSEEK_API_KEY"] == "sk-secret"
    assert "DEEPSEEK_API_KEY" in launch.secrets
    assert "sk-secret" not in " ".join(launch.command)
    assert "--api-key" not in launch.command


def test_pi_custom_endpoint_generates_models_json(monkeypatch, tmp_path):
    # pi has no --base-url flag: baseUrl -> ~/.pi/agent/models.json provider `agedum`, key by
    # $ENV name, and the model selection becomes `agedum/<model>`.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = build_launch(
        {
            "harness": "pi",
            "slug": "pi-deepseek",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            },
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert launch.command == ["pi", "--model", "agedum/deepseek-chat"]
    target, content, merge_json = launch.config_files[0]
    assert target == str(tmp_path / "pi-agent" / "models.json")
    assert merge_json is True  # augments the user's models.json, never masks it
    doc = json.loads(content)
    provider = doc["providers"]["agedum"]
    assert provider["baseUrl"] == "https://api.deepseek.com/v1"
    assert provider["api"] == "openai-completions"  # default
    assert provider["apiKey"] == "$DEEPSEEK_API_KEY"  # referenced by env-var name, not value
    assert provider["models"] == [{"id": "deepseek-chat"}]
    assert "sk-x" not in content


def test_pi_custom_endpoint_requires_model():
    with pytest.raises(ProviderError, match="pi config sets `baseUrl` but no `model`"):
        build_launch(
            {"harness": "pi", "secretEnv": "X", "config": {"baseUrl": "https://h/v1"}},
            base_env={"X": "k"},
        )


def test_pi_custom_endpoint_api_override():
    launch = build_launch(
        {
            "harness": "pi",
            "secretEnv": "ANTHROPIC_API_KEY",
            "config": {
                "baseUrl": "https://my.host/anthropic",
                "api": "anthropic-messages",
                "model": "claude-x",
            },
        },
        base_env={"ANTHROPIC_API_KEY": "sk-x"},
    )
    doc = json.loads(launch.config_files[0][1])
    assert doc["providers"]["agedum"]["api"] == "anthropic-messages"


def test_pi_keyless_endpoint_omits_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = build_launch(
        {"harness": "pi", "config": {"baseUrl": "http://localhost:1234/v1", "model": "local"}},
        base_env={},
    )
    provider = json.loads(launch.config_files[0][1])["providers"]["agedum"]
    assert "apiKey" not in provider


def test_pi_subagent_model_generates_settings(monkeypatch, tmp_path):
    # subagentModel routes every built-in pi-subagents agent via settings.json agentOverrides.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = build_launch(
        {
            "harness": "pi",
            "secretEnv": "ANTHROPIC_API_KEY",
            "config": {
                "model": "anthropic/claude-sonnet-4",
                "subagentModel": "anthropic/claude-haiku-4-5",
            },
        },
        base_env={"ANTHROPIC_API_KEY": "sk-x"},
    )
    assert launch.command == ["pi", "--model", "anthropic/claude-sonnet-4"]
    target, content, merge_json = launch.config_files[0]
    assert target == str(tmp_path / "pi-agent" / "settings.json")
    assert merge_json is True
    overrides = json.loads(content)["subagents"]["agentOverrides"]
    for agent in (
        "scout",
        "researcher",
        "planner",
        "worker",
        "reviewer",
        "context-builder",
        "oracle",
        "delegate",
    ):
        assert overrides[agent] == {"model": "anthropic/claude-haiku-4-5"}


def test_pi_subagent_model_with_custom_endpoint_routes_via_agedum(monkeypatch, tmp_path):
    # Heavy primary + fast subagent on one custom endpoint: both ids land in the models.json
    # `models` list, the subagent override routes to `agedum/<sub>`, the primary to its `<model>`.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = build_launch(
        {
            "harness": "pi",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "subagentModel": "deepseek-flash",
            },
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert launch.command == ["pi", "--model", "agedum/deepseek-chat"]
    models_doc = json.loads(launch.config_files[0][1])
    assert models_doc["providers"]["agedum"]["models"] == [
        {"id": "deepseek-chat"},
        {"id": "deepseek-flash"},
    ]
    overrides = json.loads(launch.config_files[1][1])["subagents"]["agentOverrides"]
    assert overrides["scout"] == {"model": "agedum/deepseek-flash"}


def test_pi_models_list_adds_extra_ids():
    launch = build_launch(
        {
            "harness": "pi",
            "secretEnv": "X",
            "config": {
                "baseUrl": "https://h/v1",
                "model": "m-pro",
                "models": ["m-pro", "m-flash", "m-vision"],
            },
        },
        base_env={"X": "k"},
    )
    ids = [m["id"] for m in json.loads(launch.config_files[0][1])["providers"]["agedum"]["models"]]
    assert ids == ["m-pro", "m-flash", "m-vision"]  # de-duped, model first


def test_pi_provider_def_list_cross_provider(monkeypatch, tmp_path):
    # Executor and fast subagents on DIFFERENT providers (Kimi executor + DeepSeek-flash
    # subagents): providerDef list → one models.json provider block each; model/subagentModel
    # are pi `provider/id` patterns passed through verbatim.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = build_launch(
        {
            "harness": "pi",
            "slug": "pi-kimi-flash",
            "requiredEnv": ["KIMI_API_KEY", "DEEPSEEK_API_KEY"],
            "config": {
                "model": "kimi/k2p6",
                "subagentModel": "deepseek/deepseek-v4-flash",
                "providerDef": [
                    {
                        "id": "kimi",
                        "api": "anthropic-messages",
                        "baseUrl": "https://api.kimi.com/coding",
                        "model": "k2p6",
                        "apiKeyEnv": "KIMI_API_KEY",
                    },
                    {
                        "id": "deepseek",
                        "api": "openai-completions",
                        "baseUrl": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "apiKeyEnv": "DEEPSEEK_API_KEY",
                    },
                ],
            },
        },
        base_env={"KIMI_API_KEY": "sk-kimi", "DEEPSEEK_API_KEY": "sk-ds"},
    )
    assert launch.command == ["pi", "--model", "kimi/k2p6"]
    providers = json.loads(launch.config_files[0][1])["providers"]
    assert providers["kimi"] == {
        "baseUrl": "https://api.kimi.com/coding",
        "api": "anthropic-messages",
        "apiKey": "$KIMI_API_KEY",
        "models": [{"id": "k2p6"}],
    }
    assert providers["deepseek"]["apiKey"] == "$DEEPSEEK_API_KEY"
    assert providers["deepseek"]["models"] == [{"id": "deepseek-v4-flash"}]
    overrides = json.loads(launch.config_files[1][1])["subagents"]["agentOverrides"]
    assert overrides["worker"] == {"model": "deepseek/deepseek-v4-flash"}  # verbatim, not agedum/
    # both providerDef keys are validated + exported (collected by required_env) and masked
    assert {"KIMI_API_KEY", "DEEPSEEK_API_KEY"} <= launch.secrets
    assert "sk-kimi" not in launch.config_files[0][1]


def test_pi_provider_def_single_object(monkeypatch, tmp_path):
    # A single providerDef object (not a list) is accepted too.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = build_launch(
        {
            "harness": "pi",
            "config": {
                "model": "ds/deepseek-v4-pro",
                "providerDef": {
                    "id": "ds",
                    "baseUrl": "https://api.deepseek.com",
                    "model": "deepseek-v4-pro",
                    "apiKeyEnv": "DEEPSEEK_API_KEY",
                },
            },
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert launch.command == ["pi", "--model", "ds/deepseek-v4-pro"]
    providers = json.loads(launch.config_files[0][1])["providers"]
    assert providers["ds"]["api"] == "openai-completions"  # default
    assert providers["ds"]["models"] == [{"id": "deepseek-v4-pro"}]


def test_pi_base_url_and_provider_def_mutually_exclusive():
    with pytest.raises(ProviderError, match="both `baseUrl` and `providerDef`"):
        build_launch(
            {
                "harness": "pi",
                "secretEnv": "X",
                "config": {
                    "baseUrl": "https://h/v1",
                    "model": "m",
                    "providerDef": {"id": "p", "baseUrl": "https://h2/v1", "model": "m2"},
                },
            },
            base_env={"X": "k"},
        )


def test_pi_provider_def_missing_field_fails_loudly():
    with pytest.raises(ProviderError, match="pi providerDef is missing required field"):
        build_launch(
            {
                "harness": "pi",
                "config": {"model": "p/m", "providerDef": {"id": "p", "baseUrl": "https://h/v1"}},
            },
            base_env={},
        )


# --- pi extension support: piSettings passthrough + requireExtensions warn-gate ---


def _pi_installed(tmp_path, *names):
    """A PI_CODING_AGENT_DIR whose settings.json `packages` lists `names` (so the warn-gate
    sees them as installed). Returns the agent dir path."""
    agent = tmp_path / "pi-agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "settings.json").write_text(json.dumps({"packages": [f"npm:{n}" for n in names]}))
    return agent


def test_pi_settings_passthrough(monkeypatch, tmp_path):
    # piSettings is deep-merged into a generated settings.json (any settings-based extension).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path, "pi-subagents")))
    launch = build_launch(
        {
            "harness": "pi",
            "config": {
                "model": "anthropic/claude-sonnet-4",
                "piSettings": {"subagents": {"disableBuiltins": True}, "quietStartup": True},
            },
        },
        base_env={},
    )
    target, content, merge_json = launch.config_files[0]
    assert target.endswith("settings.json")
    assert merge_json is True
    doc = json.loads(content)
    assert doc == {"subagents": {"disableBuiltins": True}, "quietStartup": True}


def test_pi_settings_composes_with_subagent_model(monkeypatch, tmp_path):
    # subagentModel is the baseline (all 8 builtins); an explicit piSettings wins on conflict.
    # ONE settings.json is emitted (not two competing files for the same target).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path, "pi-subagents")))
    launch = build_launch(
        {
            "harness": "pi",
            "config": {
                "model": "anthropic/claude-sonnet-4",
                "subagentModel": "anthropic/claude-haiku-4-5",
                "piSettings": {"subagents": {"agentOverrides": {"scout": {"thinking": "high"}}}},
            },
        },
        base_env={},
    )
    settings = [c for c in launch.config_files if c[0].endswith("settings.json")]
    assert len(settings) == 1  # composed into one fragment
    overrides = json.loads(settings[0][1])["subagents"]["agentOverrides"]
    assert overrides["worker"] == {"model": "anthropic/claude-haiku-4-5"}  # subagentModel baseline
    # piSettings merged onto the subagentModel baseline for scout (its model kept, thinking added):
    assert overrides["scout"] == {"model": "anthropic/claude-haiku-4-5", "thinking": "high"}


def test_pi_settings_must_be_object():
    with pytest.raises(ProviderError, match="piSettings.*must be a JSON object"):
        build_launch(
            {"harness": "pi", "config": {"model": "m", "piSettings": ["nope"]}}, base_env={}
        )


def test_pi_require_extensions_warns_when_missing(monkeypatch, tmp_path):
    # An explicitly required extension that isn't installed → a non-fatal warning (no raise).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path)))  # nothing installed
    launch = build_launch(
        {"harness": "pi", "config": {"model": "m", "requireExtensions": ["pi-intercom"]}},
        base_env={},
    )
    assert any("pi-intercom" in w and "not installed" in w for w in launch.warnings)


def test_pi_subagent_model_implicitly_requires_pi_subagents(monkeypatch, tmp_path):
    # subagentModel needs pi-subagents; warn when it's absent even without requireExtensions.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path)))
    launch = build_launch(
        {"harness": "pi", "config": {"model": "m", "subagentModel": "m-flash"}}, base_env={}
    )
    assert any("pi-subagents" in w for w in launch.warnings)


def test_pi_require_extensions_satisfied_no_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path, "pi-subagents")))
    launch = build_launch(
        {"harness": "pi", "config": {"model": "m", "subagentModel": "m-flash"}}, base_env={}
    )
    assert launch.warnings == ()


def test_pi_strict_extensions_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path)))
    with pytest.raises(ProviderError, match="pi-subagents.*not installed"):
        build_launch(
            {
                "harness": "pi",
                "config": {"model": "m", "subagentModel": "m-flash", "strict": True},
            },
            base_env={},
        )


def test_pi_extension_detected_via_node_modules(monkeypatch, tmp_path):
    # Installed-detection also works from the npm/node_modules dir, not just settings packages.
    agent = tmp_path / "pi-agent"
    (agent / "npm" / "node_modules" / "pi-subagents").mkdir(parents=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    launch = build_launch(
        {"harness": "pi", "config": {"model": "m", "subagentModel": "m-flash"}}, base_env={}
    )
    assert launch.warnings == ()


def test_pi_extension_config_writes_extension_own_file(monkeypatch, tmp_path):
    # piExtensionConfig reaches an extension's OWN file (not settings.json) — e.g.
    # pi-subagents' parallel/async knobs in extensions/subagent/config.json.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path, "pi-subagents")))
    launch = build_launch(
        {
            "harness": "pi",
            "config": {
                "model": "m",
                "piExtensionConfig": {
                    "extensions/subagent/config.json": {
                        "parallel": {"maxTasks": 12, "concurrency": 6},
                        "asyncByDefault": True,
                    }
                },
            },
        },
        base_env={},
    )
    agent = tmp_path / "pi-agent"
    entry = next(c for c in launch.config_files if c[0].endswith("subagent/config.json"))
    target, content, merge_json = entry
    assert target == str(agent / "extensions" / "subagent" / "config.json")
    assert merge_json is True
    assert json.loads(content) == {
        "parallel": {"maxTasks": 12, "concurrency": 6},
        "asyncByDefault": True,
    }


def test_pi_extension_config_composes_with_settings_and_models(monkeypatch, tmp_path):
    # All three generated files coexist: models.json (baseUrl), settings.json (subagentModel),
    # and the extension-own file (piExtensionConfig).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(_pi_installed(tmp_path, "pi-subagents")))
    launch = build_launch(
        {
            "harness": "pi",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-v4-pro",
                "subagentModel": "deepseek-v4-flash",
                "piExtensionConfig": {"extensions/subagent/config.json": {"asyncByDefault": True}},
            },
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    targets = [c[0] for c in launch.config_files]
    assert any(t.endswith("models.json") for t in targets)
    assert any(t.endswith("settings.json") for t in targets)
    assert any(t.endswith("subagent/config.json") for t in targets)


def test_pi_extension_config_rejects_managed_targets():
    for target in ("settings.json", "models.json"):
        with pytest.raises(ProviderError, match="agedum-managed"):
            build_launch(
                {
                    "harness": "pi",
                    "config": {"model": "m", "piExtensionConfig": {target: {"x": 1}}},
                },
                base_env={},
            )


def test_pi_extension_config_rejects_unsafe_paths():
    for bad in ("../escape.json", "/etc/passwd", "a/../../b.json"):
        with pytest.raises(ProviderError, match="relative path under"):
            build_launch(
                {"harness": "pi", "config": {"model": "m", "piExtensionConfig": {bad: {"x": 1}}}},
                base_env={},
            )


def test_pi_extension_config_value_must_be_object():
    with pytest.raises(ProviderError, match="must be a JSON object"):
        build_launch(
            {
                "harness": "pi",
                "config": {"model": "m", "piExtensionConfig": {"extensions/x/config.json": 5}},
            },
            base_env={},
        )


# --- with_prompt: per-harness prompt seeding (--prompt / --run) ---


def _launch(harness, command):
    return Launch(harness=harness, label=harness, command=command)


def test_with_prompt_claude_interactive_is_positional():
    # claude seeds an interactive session from a positional prompt.
    assert with_prompt(_launch("claude", ["claude"]), [], "hello", interactive=True) == [
        "claude",
        "hello",
    ]


def test_with_prompt_claude_run_uses_print():
    # --print is claude's non-interactive (run-and-exit) mode.
    assert with_prompt(_launch("claude", ["claude"]), [], "hello", interactive=False) == [
        "claude",
        "--print",
        "hello",
    ]


def test_with_prompt_kimi_interactive_fails_loudly():
    # Kimi Code's --prompt runs once and exits; there is no seed-then-stay mode.
    with pytest.raises(ProviderError, match="no interactive prompt-seeding"):
        with_prompt(_launch("kimi", ["kimi", "--model", "k"]), [], "hi", interactive=True)


def test_with_prompt_kimi_run_uses_prompt_and_drops_interactive_flags():
    # kimi --run: --prompt runs the task non-interactively (no --print any more). Kimi Code
    # rejects --prompt combined with --yolo/--auto (and --plan is interactive-only), so the
    # seed command strips them, keeping --model.
    assert with_prompt(
        _launch("kimi", ["kimi", "--model", "k", "--yolo"]), [], "hi", interactive=False
    ) == [
        "kimi",
        "--model",
        "k",
        "--prompt",
        "hi",
    ]


def test_with_prompt_opencode_interactive_uses_prompt_flag():
    assert with_prompt(_launch("opencode", ["opencode"]), [], "hi", interactive=True) == [
        "opencode",
        "--prompt",
        "hi",
    ]


def test_with_prompt_opencode_run_uses_run_subcommand_first():
    # opencode's `run` subcommand must lead, before the message.
    assert with_prompt(_launch("opencode", ["opencode"]), [], "hi", interactive=False) == [
        "opencode",
        "run",
        "hi",
    ]


def test_with_prompt_preserves_passthrough_args():
    seeded = with_prompt(_launch("claude", ["claude"]), ["--add-dir", "/x"], "hi", interactive=True)
    assert seeded == ["claude", "--add-dir", "/x", "hi"]
    # passthrough lands after the `run` subcommand, before the message
    ran = with_prompt(_launch("opencode", ["opencode"]), ["-m", "p/m"], "hi", interactive=False)
    assert ran == ["opencode", "run", "-m", "p/m", "hi"]


def test_with_prompt_unknown_harness_fails_loudly():
    with pytest.raises(ProviderError, match="no known prompt-seeding flags"):
        with_prompt(_launch("mystery", ["mystery"]), [], "hi", interactive=True)


def test_with_prompt_cline_interactive_uses_tui_flag():
    # cline: --tui opens the interactive TUI seeded with the positional prompt; base flags
    # from _cline_env (here --model) are preserved and the seed text stays last.
    cmd = with_prompt(_launch("cline", ["cline", "--model", "x"]), [], "hi", interactive=True)
    assert cmd == ["cline", "--model", "x", "--tui", "hi"]


def test_with_prompt_cline_run_uses_bare_positional():
    # cline: a bare positional prompt runs once in act mode and exits (no --tui).
    assert with_prompt(_launch("cline", ["cline"]), [], "hi", interactive=False) == [
        "cline",
        "hi",
    ]


def test_with_prompt_reasonix_run_swaps_chat_for_run():
    # reasonix --run: the base `chat` subcommand becomes `run`, --model preserved, text last.
    cmd = with_prompt(
        _launch("reasonix", ["reasonix", "chat", "--model", "deepseek-pro"]),
        [],
        "fix the bug",
        interactive=False,
    )
    assert cmd == ["reasonix", "run", "--model", "deepseek-pro", "fix the bug"]


def test_with_prompt_reasonix_run_without_model():
    assert with_prompt(_launch("reasonix", ["reasonix", "chat"]), [], "go", interactive=False) == [
        "reasonix",
        "run",
        "go",
    ]


def test_with_prompt_reasonix_interactive_fails_loudly():
    # reasonix `chat` can't be pre-seeded, so --prompt (interactive) is a fail-loud error.
    with pytest.raises(ProviderError, match="no interactive prompt-seeding"):
        with_prompt(_launch("reasonix", ["reasonix", "chat"]), [], "hi", interactive=True)


def test_with_prompt_aider_run_uses_message():
    # aider --run: --message runs once and exits; base flags from _aider_env are preserved.
    cmd = with_prompt(
        _launch("aider", ["aider", "--model", "m", "--no-git"]),
        [],
        "fix the bug",
        interactive=False,
    )
    assert cmd == ["aider", "--model", "m", "--no-git", "--message", "fix the bug"]


def test_with_prompt_aider_run_preserves_passthrough():
    cmd = with_prompt(
        _launch("aider", ["aider"]), ["--map-tokens", "1024"], "go", interactive=False
    )
    assert cmd == ["aider", "--map-tokens", "1024", "--message", "go"]


def test_with_prompt_aider_interactive_fails_loudly():
    # aider's --message exits; there is no interactive prompt-seed, so --prompt fails loudly.
    with pytest.raises(ProviderError, match="no interactive prompt-seeding"):
        with_prompt(_launch("aider", ["aider", "--no-git"]), [], "hi", interactive=True)


def test_with_prompt_pi_interactive_is_positional():
    # pi seeds an interactive TUI from a positional prompt; base flags preserved.
    cmd = with_prompt(_launch("pi", ["pi", "--model", "m"]), [], "hello", interactive=True)
    assert cmd == ["pi", "--model", "m", "hello"]


def test_with_prompt_pi_run_uses_print():
    # --print is pi's non-interactive (run-and-exit) mode; the prompt stays positional.
    assert with_prompt(_launch("pi", ["pi"]), [], "hello", interactive=False) == [
        "pi",
        "--print",
        "hello",
    ]


def test_with_prompt_pi_preserves_passthrough():
    cmd = with_prompt(_launch("pi", ["pi"]), ["--no-skills"], "go", interactive=False)
    assert cmd == ["pi", "--no-skills", "--print", "go"]


# --- codex (OpenAI Codex CLI) ---


def test_codex_basic_model_flag():
    # model is a plain -m flag; no custom provider and no on-disk config without baseUrl.
    launch = build_launch(
        {"harness": "codex", "config": {"model": "gpt-5.5"}},
        base_env={},
    )
    assert launch.command == ["codex", "-m", "gpt-5.5"]
    assert launch.config_files == ()


def test_codex_bare_runs_codex():
    launch = build_launch({"harness": "codex", "config": {}}, base_env={})
    assert launch.command == ["codex"]
    assert launch.config_files == ()


def test_codex_custom_endpoint_passes_provider_overrides():
    # baseUrl -> a [model_providers.agedum] block via -c overrides, selected with -c
    # model_provider=agedum, plus -m <model>. No file is generated. wire_api is NOT emitted
    # without wireApi, so codex's own default (the Responses API) applies.
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"baseUrl": "https://proxy.local/v1", "model": "deepseek-v4-pro"},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert launch.command == [
        "codex",
        "-c",
        'model_provider="agedum"',
        "-c",
        'model_providers.agedum.name="agedum"',
        "-c",
        'model_providers.agedum.base_url="https://proxy.local/v1"',
        "-c",
        'model_providers.agedum.env_key="DEEPSEEK_API_KEY"',
        "-m",
        "deepseek-v4-pro",
    ]
    assert not any("wire_api" in token for token in launch.command)
    assert launch.config_files == ()


def test_codex_wire_api_override():
    # wireApi is emitted only when explicitly set.
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "OPENAI_API_KEY",
            "config": {
                "baseUrl": "https://example.com/v1",
                "model": "gpt-5.5",
                "wireApi": "responses",
            },
        },
        base_env={"OPENAI_API_KEY": "sk-x"},
    )
    assert 'model_providers.agedum.wire_api="responses"' in launch.command


def test_codex_config_passthrough_typed_scalars():
    # codexConfig -> `-c key=<toml>` overrides: int bare, bool bare, string quoted — so codex
    # parses each value at the type the setting expects.
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "KIMI_API_KEY",
            "config": {
                "baseUrl": "https://api.kimi.com/coding/v1",
                "chatCompletions": True,
                "model": "kimi-for-coding",
                "codexConfig": {
                    "model_context_window": 262144,
                    "model_supports_reasoning_summaries": True,
                    "model_reasoning_summary": "auto",
                },
            },
        },
        base_env={"KIMI_API_KEY": "sk-x"},
    )
    command = launch.command
    assert "model_context_window=262144" in command
    assert "model_supports_reasoning_summaries=true" in command
    assert 'model_reasoning_summary="auto"' in command
    # The overrides land after -m, each preceded by its own -c.
    for token in (
        "model_context_window=262144",
        "model_supports_reasoning_summaries=true",
        'model_reasoning_summary="auto"',
    ):
        assert command[command.index(token) - 1] == "-c"


def test_codex_config_passthrough_nested_tables_flatten_to_dotted_keys():
    # a nested codexConfig table (e.g. [sandbox_workspace_write]) becomes dotted-key
    # -c overrides — the same shape the mcp_servers translation emits — with lists as
    # TOML arrays and bools bare.
    launch = build_launch(
        {
            "harness": "codex",
            "config": {
                "codexConfig": {
                    "sandbox_mode": "workspace-write",
                    "sandbox_workspace_write": {
                        "writable_roots": ["/home/alice/src/worktrees"],
                        "network_access": True,
                    },
                }
            },
        },
        base_env={},
    )
    command = launch.command
    assert 'sandbox_mode="workspace-write"' in command
    assert 'sandbox_workspace_write.writable_roots=["/home/alice/src/worktrees"]' in command
    assert "sandbox_workspace_write.network_access=true" in command


def test_codex_config_rejects_non_table():
    with pytest.raises(ProviderError, match="codexConfig"):
        build_launch(
            {"harness": "codex", "config": {"model": "m", "codexConfig": ["nope"]}},
            base_env={},
        )


def test_codex_mcp_servers_stdio():
    # canonical mcpServers -> `-c mcp_servers.<name>…` overrides: command quoted, args as a
    # TOML array; each override preceded by its own -c.
    launch = build_launch(
        {
            "harness": "codex",
            "config": {
                "mcpServers": {
                    "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]},
                }
            },
        },
        base_env={},
    )
    command = launch.command
    for token in (
        'mcp_servers.context7.command="npx"',
        'mcp_servers.context7.args=["-y", "@upstash/context7-mcp@latest"]',
    ):
        assert token in command
        assert command[command.index(token) - 1] == "-c"


def test_codex_mcp_servers_env_cwd_remote():
    launch = build_launch(
        {
            "harness": "codex",
            "config": {
                "mcpServers": {
                    "stdio": {"command": "my-mcp", "env": {"K": "v"}, "cwd": "/tmp"},
                    "remote": {
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"Authorization": "Bearer tok"},
                    },
                }
            },
        },
        base_env={},
    )
    command = launch.command
    for token in (
        'mcp_servers.stdio.command="my-mcp"',
        'mcp_servers.stdio.env.K="v"',
        'mcp_servers.stdio.cwd="/tmp"',
        'mcp_servers.remote.url="https://mcp.example.com/mcp"',
        'mcp_servers.remote.headers={"Authorization" = "Bearer tok"}',
    ):
        assert token in command


def test_codex_mcp_placeholder_rejected():
    with pytest.raises(ProviderError, match=r"mcpServers\.ctx"):
        build_launch(
            {
                "harness": "codex",
                "config": {"mcpServers": {"ctx": {"command": "npx", "args": ["${TOKEN}"]}}},
            },
            base_env={},
        )


_FAKE_CATALOG = {
    "models": [
        {
            "slug": "gpt-tmpl",
            "display_name": "Template",
            "description": "d",
            "base_instructions": "You are a coding agent.",
            "context_window": 272000,
            "max_context_window": 272000,
            "supports_reasoning_summaries": True,
            "default_reasoning_summary": "none",
            "availability_nux": {"message": "new!"},
            "upgrade": {"to": "x"},
        }
    ]
}


def test_codex_model_catalog_clones_template_and_wires_flag(monkeypatch):
    # codexModelCatalog -> agedum clones the live catalog's first entry as the config's model,
    # applies contextWindow/displayName, writes a model_catalog_json file, and points codex at it.
    monkeypatch.setattr("agedum.provider._codex_debug_models", lambda: _FAKE_CATALOG)
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "KIMI_API_KEY",
            "config": {
                "baseUrl": "https://api.kimi.com/coding/v1",
                "chatCompletions": True,
                "model": "kimi-for-coding",
                "codexModelCatalog": {"contextWindow": 262144, "displayName": "Kimi K2.7 (Code)"},
            },
        },
        base_env={"KIMI_API_KEY": "sk-x"},
    )
    catalog_files = [
        cf for cf in launch.config_files if cf[0].endswith("agedum-model-catalog.json")
    ]
    assert len(catalog_files) == 1
    target, content, is_project = catalog_files[0]
    assert is_project is False
    doc = json.loads(content)
    entry = doc["models"][0]
    assert entry["slug"] == "kimi-for-coding"
    assert entry["display_name"] == "Kimi K2.7 (Code)"
    assert entry["context_window"] == 262144
    assert entry["max_context_window"] == 262144
    assert entry["base_instructions"] == "You are a coding agent."  # version-correct template field
    assert "availability_nux" not in entry and "upgrade" not in entry
    assert f'model_catalog_json="{target}"' in " ".join(launch.command)


def test_codex_model_catalog_skipped_when_codex_unavailable(monkeypatch):
    # If `codex debug models` can't be queried, the catalog is skipped — the launch still works
    # (codex falls back to its own metadata), no file, no flag.
    monkeypatch.setattr("agedum.provider._codex_debug_models", lambda: None)
    launch = build_launch(
        {
            "harness": "codex",
            "config": {"model": "kimi-for-coding", "codexModelCatalog": {"contextWindow": 262144}},
        },
        base_env={},
    )
    assert not any(cf[0].endswith("agedum-model-catalog.json") for cf in launch.config_files)
    assert not any("model_catalog_json" in token for token in launch.command)


def test_codex_model_catalog_rejects_non_table(monkeypatch):
    monkeypatch.setattr("agedum.provider._codex_debug_models", lambda: _FAKE_CATALOG)
    with pytest.raises(ProviderError, match="codexModelCatalog"):
        build_launch(
            {"harness": "codex", "config": {"model": "m", "codexModelCatalog": "nope"}},
            base_env={},
        )


def test_codex_key_via_env_export_not_argv():
    # The key reaches codex via the required-env export (its conventional name, referenced as
    # the provider's env_key), never argv.
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-secret"},
    )
    assert launch.env["DEEPSEEK_API_KEY"] == "sk-secret"
    assert "DEEPSEEK_API_KEY" in launch.secrets
    assert "sk-secret" not in " ".join(launch.command)


def test_codex_subagent_model_generates_flash_agent_file(monkeypatch, tmp_path):
    # subagentModel -> a generated ~/.codex/agents/flash.toml custom-agent (the fast model),
    # written verbatim (not JSON-merged). codex has no global subagent-model knob.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
                "subagentModel": "deepseek-v4-flash",
            },
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert len(launch.config_files) == 1
    target, content, merge_json = launch.config_files[0]
    assert target == str(tmp_path / "codex-home" / "agents" / "flash.toml")
    assert merge_json is False
    assert 'name = "flash"' in content
    assert 'model = "deepseek-v4-flash"' in content
    assert "developer_instructions" in content
    # agedum injects its confined-launch sandbox default when the agent omits sandbox_mode.
    assert 'sandbox_mode = "workspace-write"' in content
    # The executor still launches on the primary model.
    assert launch.command[-2:] == ["-m", "deepseek-v4-pro"]


def _write_codex_agent(directory, name, body):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body)
    return path


def test_codex_agents_bind_personal_scope(monkeypatch, tmp_path):
    # codexAgents -> every *.toml in the source dir is bound into ~/.codex/agents/<name>.toml.
    # sandbox_mode is injected when the source omits it, passed through when set.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    src = tmp_path / "agents"
    _write_codex_agent(src, "worker", 'name = "worker"\nmodel = "deepseek-v4-flash"\n')
    reviewer_body = 'name = "reviewer"\nmodel = "deepseek-v4-pro"\nsandbox_mode = "read-only"\n'
    _write_codex_agent(src, "reviewer", reviewer_body)
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"model": "deepseek-v4-pro", "codexAgents": str(src)},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    by_target = {target: content for target, content, _ in launch.config_files}
    agents_dir = tmp_path / "codex-home" / "agents"
    assert set(by_target) == {str(agents_dir / "worker.toml"), str(agents_dir / "reviewer.toml")}
    # injected default for the agent that omitted sandbox_mode:
    assert 'sandbox_mode = "workspace-write"' in by_target[str(agents_dir / "worker.toml")]
    # explicit sandbox_mode passed through, not doubled:
    reviewer = by_target[str(agents_dir / "reviewer.toml")]
    assert 'sandbox_mode = "read-only"' in reviewer
    assert "workspace-write" not in reviewer


def test_codex_project_agents_bind_relative_target(tmp_path):
    # codexProjectAgents -> a project-relative target (.codex/agents/<name>.toml) so the bind
    # lands in the working tree and assert_safe's git-tracked-target guard applies.
    src = tmp_path / "proj-agents"
    _write_codex_agent(src, "tester", 'name = "tester"\nmodel = "deepseek-v4-flash"\n')
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"model": "deepseek-v4-pro", "codexProjectAgents": str(src)},
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert len(launch.config_files) == 1
    target, _, merge_json = launch.config_files[0]
    assert target == ".codex/agents/tester.toml"
    assert merge_json is False


def test_codex_agents_duplicate_target_raises(monkeypatch, tmp_path):
    # A codexAgents flash.toml colliding with the subagentModel flash.toml is a hard error.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    src = tmp_path / "agents"
    _write_codex_agent(src, "flash", 'name = "flash"\nmodel = "deepseek-v4-flash"\n')
    with pytest.raises(ProviderError, match="duplicate codex agent target"):
        build_launch(
            {
                "harness": "codex",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {
                    "model": "deepseek-v4-pro",
                    "subagentModel": "deepseek-v4-flash",
                    "codexAgents": str(src),
                },
            },
            base_env={"DEEPSEEK_API_KEY": "sk-x"},
        )


def test_codex_agents_missing_dir_raises():
    with pytest.raises(ProviderError, match="is not a directory"):
        build_launch(
            {
                "harness": "codex",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"model": "deepseek-v4-pro", "codexAgents": "/no/such/agents/dir"},
            },
            base_env={"DEEPSEEK_API_KEY": "sk-x"},
        )


def test_codex_chat_completions_interposes_proxy_upstream():
    # chatCompletions: true -> the upstream is signaled via AGEDUM_CODEX_CHAT_UPSTREAM (the CLI
    # then interposes a Responses<->Chat proxy); no wire_api override is emitted (codex speaks
    # the Responses API to the proxy). The base_url override still carries the real upstream —
    # the CLI rewrites it to the proxy address at launch.
    launch = build_launch(
        {
            "harness": "codex",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-pro",
                "chatCompletions": True,
            },
        },
        base_env={"DEEPSEEK_API_KEY": "sk-x"},
    )
    assert launch.env["AGEDUM_CODEX_CHAT_UPSTREAM"] == "https://api.deepseek.com/v1"
    assert not any("wire_api" in token for token in launch.command)
    assert 'model_providers.agedum.base_url="https://api.deepseek.com/v1"' in launch.command


def test_with_prompt_codex_interactive_is_positional():
    # codex seeds an interactive session from a positional prompt; base flags preserved.
    cmd = with_prompt(_launch("codex", ["codex", "-m", "m"]), [], "hello", interactive=True)
    assert cmd == ["codex", "-m", "m", "hello"]


def test_with_prompt_codex_run_uses_exec():
    # The `exec` subcommand runs once non-interactively; it leads, before flags and the prompt.
    cmd = with_prompt(_launch("codex", ["codex", "-m", "m"]), [], "hello", interactive=False)
    assert cmd == ["codex", "exec", "-m", "m", "hello"]


def test_with_prompt_codex_preserves_passthrough():
    cmd = with_prompt(_launch("codex", ["codex"]), ["--full-auto"], "go", interactive=False)
    assert cmd == ["codex", "exec", "--full-auto", "go"]


# --- sandbox (write-confinement) ---


def test_build_launch_parses_sandbox():
    config = {
        "harness": "claude",
        "config": {},
        "sandbox": {"readWrite": ["~/data", "${PROJECT_ROOT}/out"]},
    }
    launch = build_launch(config, {})
    assert launch.sandbox is not None
    assert launch.sandbox.enabled
    assert launch.sandbox.read_write == ("~/data", "${PROJECT_ROOT}/out")


def test_build_launch_without_sandbox_is_none():
    assert build_launch({"harness": "claude", "config": {}}, {}).sandbox is None


def test_sandbox_empty_block_enables_with_no_extra_rw():
    # `"sandbox": {}` still confines (host read-only); only the auto-writable set applies.
    launch = build_launch({"harness": "claude", "config": {}, "sandbox": {}}, {})
    assert launch.sandbox is not None
    assert launch.sandbox.enabled
    assert launch.sandbox.read_write == ()


def test_sandbox_block_must_be_an_object():
    with pytest.raises(ProviderError, match="`sandbox` must be a JSON object"):
        build_launch({"harness": "claude", "config": {}, "sandbox": []}, {})


def test_sandbox_read_write_must_be_a_list_of_strings():
    with pytest.raises(ProviderError, match="readWrite"):
        build_launch({"harness": "claude", "config": {}, "sandbox": {"readWrite": "nope"}}, {})
    with pytest.raises(ProviderError, match="readWrite"):
        build_launch({"harness": "claude", "config": {}, "sandbox": {"readWrite": [1]}}, {})


# --- config dirs + extends ---


def test_load_merged_config_single_extends(tmp_path):
    base = {"abstract": True, "harness": "claude", "config": {"baseUrl": "u", "effort": "max"}}
    _write_config(tmp_path, "base/claude.json", base)
    child = {"extends": "base/claude.json", "config": {"model": "pro"}}
    merged = load_merged_config(_write_config(tmp_path, "claude/deepseek.json", child), tmp_path)
    # base + child config deep-merged; meta keys stripped; abstract NOT inherited.
    assert merged == {
        "harness": "claude",
        "config": {"baseUrl": "u", "effort": "max", "model": "pro"},
    }


def test_load_merged_config_child_overrides_base(tmp_path):
    _write_config(tmp_path, "base/c.json", {"harness": "claude", "config": {"model": "b", "x": 1}})
    child = {"extends": "base/c.json", "config": {"model": "child"}}
    merged = load_merged_config(_write_config(tmp_path, "c.json", child), tmp_path)
    assert merged["config"] == {"model": "child", "x": 1}


def test_load_merged_config_list_extends_left_to_right(tmp_path):
    _write_config(tmp_path, "a.json", {"harness": "claude", "config": {"x": 1, "y": 1}})
    _write_config(tmp_path, "b.json", {"config": {"y": 2, "z": 2}})
    child = {"extends": ["a.json", "b.json"], "config": {"z": 3}}
    merged = load_merged_config(_write_config(tmp_path, "child.json", child), tmp_path)
    # a, then b over a (y), then child over both (z).
    assert merged["config"] == {"x": 1, "y": 2, "z": 3}


def test_load_merged_config_unions_required_env(tmp_path):
    # A plain deep-merge replaces lists, so a child declaring its own requiredEnv would drop
    # the base's — launching with the base's token neither validated nor exported, and
    # whatever the base configured with it failing at first use instead of at launch.
    _write_config(
        tmp_path,
        "base/mcp.json",
        {"abstract": True, "harness": "opencode", "requiredEnv": ["NODUM_AGENT_TOKEN"]},
    )
    child = {"extends": "base/mcp.json", "requiredEnv": ["BUFFER_KEY"]}
    merged = load_merged_config(_write_config(tmp_path, "oc/x.json", child), tmp_path)
    assert merged["requiredEnv"] == ["NODUM_AGENT_TOKEN", "BUFFER_KEY"]


def test_load_merged_config_required_env_union_dedupes(tmp_path):
    _write_config(tmp_path, "a.json", {"harness": "claude", "requiredEnv": ["K", "A"]})
    _write_config(tmp_path, "b.json", {"requiredEnv": ["K", "B"]})
    child = {"extends": ["a.json", "b.json"], "requiredEnv": ["A", "C"]}
    merged = load_merged_config(_write_config(tmp_path, "child.json", child), tmp_path)
    assert merged["requiredEnv"] == ["K", "A", "B", "C"]


def test_load_merged_config_is_recursive(tmp_path):
    _write_config(tmp_path, "grand.json", {"harness": "claude", "config": {"a": 1}})
    _write_config(tmp_path, "mid.json", {"extends": "grand.json", "config": {"b": 2}})
    child = _write_config(tmp_path, "child.json", {"extends": "mid.json", "config": {"c": 3}})
    assert load_merged_config(child, tmp_path)["config"] == {"a": 1, "b": 2, "c": 3}


def test_load_merged_config_absolute_extends(tmp_path):
    base = _write_config(tmp_path, "elsewhere/base.json", {"harness": "claude", "config": {"a": 1}})
    child = _write_config(tmp_path, "child.json", {"extends": str(base)})
    assert load_merged_config(child, tmp_path)["config"] == {"a": 1}


def test_load_merged_config_missing_base_errors(tmp_path):
    child = _write_config(tmp_path, "child.json", {"extends": "nope.json"})
    with pytest.raises(ProviderError, match="cannot read"):
        load_merged_config(child, tmp_path)


def test_load_merged_config_cycle_errors(tmp_path):
    _write_config(tmp_path, "a.json", {"extends": "b.json"})
    _write_config(tmp_path, "b.json", {"extends": "a.json"})
    with pytest.raises(ProviderError, match="circular"):
        load_merged_config(tmp_path / "a.json", tmp_path)


def test_extends_must_be_string_or_list(tmp_path):
    child = _write_config(tmp_path, "child.json", {"extends": 5})
    with pytest.raises(ProviderError, match="extends"):
        load_merged_config(child, tmp_path)


def test_list_providers_recursive_skips_abstract(tmp_path):
    base = {"abstract": True, "harness": "claude", "config": {"model": "x"}}
    _write_config(tmp_path, "base/claude.json", base)
    child = {"extends": "base/claude.json", "config": {"model": "pro"}}
    _write_config(tmp_path, "claude/deepseek.json", child)
    _write_config(tmp_path, "top.json", {"harness": "kimi", "config": {"model": "k"}})
    by_name = {s.name: s for s in list_providers(tmp_path)}
    assert set(by_name) == {"claude/deepseek", "top"}  # abstract base skipped
    # harness/model come from the effective (extends-resolved) config.
    assert by_name["claude/deepseek"].harness == "claude"
    assert by_name["claude/deepseek"].model == "pro"


def test_build_launch_uses_given_label():
    launch = build_launch({"harness": "claude", "config": {}}, {}, label="claude/deepseek")
    assert launch.label == "claude/deepseek"


# --- YAML source format ---


def _write_yaml(root, rel, text):
    """Write a YAML config at ``root/rel`` (creating parents); return its path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _sample_envelope():
    """A representative provider envelope shared by the JSON↔YAML parity tests."""
    return {
        "harness": "claude",
        "secretEnv": "DEEPSEEK_API_KEY",
        "requiredEnv": ["DEEPSEEK_API_KEY"],
        "config": {
            "baseUrl": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro",
            "authStyle": "apikey",
            "mcpServers": {
                "nodum": {
                    "command": "nodum",
                    "args": ["mcp", "serve"],
                    "env": {"NODUM_AGENT_TOKEN": "${NODUM_AGENT_TOKEN}"},
                }
            },
        },
    }


def _sample_envelope_yaml():
    """The YAML spelling of :func:`_sample_envelope` (plus the required schema key)."""
    return """\
schema: agedum-provider/v1
harness: claude
secretEnv: DEEPSEEK_API_KEY
requiredEnv:
  - DEEPSEEK_API_KEY
config:
  baseUrl: https://api.deepseek.com/anthropic
  model: deepseek-v4-pro
  authStyle: apikey
  mcpServers:
    nodum:
      command: nodum
      args: [mcp, serve]
      env:
        NODUM_AGENT_TOKEN: ${NODUM_AGENT_TOKEN}
"""


def test_yaml_config_loads_to_the_json_equivalent(tmp_path):
    # Parse, not translate: the same envelope in either format yields one dict.
    _write_config(tmp_path, "j.json", _sample_envelope())
    _write_yaml(tmp_path, "y.yaml", _sample_envelope_yaml())
    assert load_config(tmp_path / "y.yaml") == load_config(tmp_path / "j.json")


def test_load_config_with_format_reports_the_source_format(tmp_path):
    _write_config(tmp_path, "j.json", _sample_envelope())
    _write_yaml(tmp_path, "y.yaml", _sample_envelope_yaml())
    _write_yaml(tmp_path, "y.yml", _sample_envelope_yaml())
    assert load_config_with_format(tmp_path / "j.json").format == "json"
    assert load_config_with_format(tmp_path / "y.yaml").format == "yaml"
    assert load_config_with_format(tmp_path / "y.yml").format == "yaml"


def test_yaml_schema_key_is_required(tmp_path):
    _write_yaml(tmp_path, "x.yaml", "harness: claude\n")
    with pytest.raises(ProviderSchemaError, match="agedum-provider/v1"):
        load_config(tmp_path / "x.yaml")


def test_yaml_schema_key_wrong_value_names_the_expected_version(tmp_path):
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v2\nharness: claude\n")
    with pytest.raises(ProviderSchemaError, match=r"`schema: agedum-provider/v1`"):
        load_config(tmp_path / "x.yaml")


def test_yaml_schema_key_is_stripped_from_the_loaded_dict(tmp_path):
    # JSON needs no version key, so the YAML form must not land one in the dict either.
    _write_yaml(tmp_path, "x.yaml", _sample_envelope_yaml())
    assert "schema" not in load_config(tmp_path / "x.yaml")


def test_json_config_loads_exactly_as_before(tmp_path):
    # JSON is the legacy format: no schema key required, none stripped.
    path = _write_config(tmp_path, "x.json", _sample_envelope())
    assert load_config(path) == _sample_envelope()


def test_invalid_yaml_errors(tmp_path):
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\nharness: [claude\n")
    with pytest.raises(ProviderError, match="invalid YAML"):
        load_config(tmp_path / "x.yaml")


def test_non_mapping_yaml_errors(tmp_path):
    _write_yaml(tmp_path, "x.yaml", "- a\n- b\n")
    with pytest.raises(ProviderError, match="YAML mapping"):
        load_config(tmp_path / "x.yaml")


# --- YAML 1.1 boolean traps ---


def test_yaml_boolean_trap_in_secret_env(tmp_path):
    for word in ("on", "yes", "no", "off"):
        _write_yaml(tmp_path, "x.yaml", f"schema: agedum-provider/v1\nsecretEnv: {word}\n")
        with pytest.raises(
            YamlBooleanTrapError,
            match=r"yaml boolean trap at secretEnv.*parsed as boolean.*quote the value",
        ):
            load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_required_env_entry(tmp_path):
    _write_yaml(
        tmp_path, "x.yaml", "schema: agedum-provider/v1\nrequiredEnv: [DEEPSEEK_API_KEY, no]\n"
    )
    with pytest.raises(YamlBooleanTrapError, match=r"at requiredEnv\[1\]"):
        load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_env_value(tmp_path):
    _write_yaml(
        tmp_path,
        "x.yaml",
        "schema: agedum-provider/v1\nconfig:\n  extraEnv:\n    FOO: no\n",
    )
    with pytest.raises(YamlBooleanTrapError, match=r"at config\.extraEnv\.FOO"):
        load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_mcp_env_value(tmp_path):
    _write_yaml(
        tmp_path,
        "x.yaml",
        "schema: agedum-provider/v1\n"
        "config:\n"
        "  mcpServers:\n"
        "    nodum:\n"
        "      command: nodum\n"
        "      env:\n"
        "        TOKEN: yes\n",
    )
    with pytest.raises(YamlBooleanTrapError, match=r"at config\.mcpServers\.nodum\.env\.TOKEN"):
        load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_extends_ref(tmp_path):
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\nextends: on\n")
    with pytest.raises(YamlBooleanTrapError, match="at extends"):
        load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_compaction_enum(tmp_path):
    # cline's `compaction: off` is the exact trap: unquoted, `off` parses as False and
    # would reach the consumer as the string "False".
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\nconfig:\n  compaction: off\n")
    with pytest.raises(YamlBooleanTrapError, match="at config.compaction"):
        load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_opencode_default_options(tmp_path):
    # _clean_options silently drops any non-string, so an unquoted trap word here would
    # make the option silently absent instead of failing the load.
    for key in ("reasoningEffort", "textVerbosity", "reasoningSummary"):
        _write_yaml(
            tmp_path,
            "x.yaml",
            "schema: agedum-provider/v1\n"
            "harness: opencode\n"
            "config:\n"
            "  defaultOptions:\n"
            f"    {key}: off\n",
        )
        with pytest.raises(YamlBooleanTrapError, match=rf"at config\.defaultOptions\.{key}"):
            load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_opencode_agent_options_row(tmp_path):
    # Each agentOptions row is consumed as `agent` (the agent name) + `model` + the same
    # three option keys via _clean_options — a bool in any of them misbehaves silently.
    row_fixtures = {
        "agent": "    - agent: on\n      model: built-in/build\n",
        "model": "    - agent: plan\n      model: off\n",
        "reasoningEffort": (
            "    - agent: plan\n      model: built-in/build\n      reasoningEffort: no\n"
        ),
    }
    for key, row_text in row_fixtures.items():
        _write_yaml(
            tmp_path,
            "x.yaml",
            "schema: agedum-provider/v1\nharness: opencode\nconfig:\n  agentOptions:\n" + row_text,
        )
        with pytest.raises(YamlBooleanTrapError, match=rf"at config\.agentOptions\[0\]\.{key}"):
            load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_opencode_provider_def_npm_and_name(tmp_path):
    # providerDef.npm / .name are read as strings by _apply_provider_def: a bool `npm`
    # surfaces as the confusing "missing required field(s): npm", a bool `name` bakes
    # "False" into the generated config doc.
    for key, word in (("npm", "on"), ("name", "no")):
        _write_yaml(
            tmp_path,
            "x.yaml",
            "schema: agedum-provider/v1\n"
            "harness: opencode\n"
            "config:\n"
            "  providerDef:\n"
            "    id: external\n"
            f"    {key}: {word}\n"
            "    baseUrl: https://x/v1\n"
            "    model: m\n"
            "    apiKeyEnv: K\n",
        )
        with pytest.raises(YamlBooleanTrapError, match=rf"at config\.providerDef\[0\]\.{key}"):
            load_config(tmp_path / "x.yaml")


def test_yaml_boolean_trap_in_codex_wire_api(tmp_path):
    # A non-string wireApi is silently dropped by _codex_env, losing the wire override.
    _write_yaml(
        tmp_path,
        "x.yaml",
        "schema: agedum-provider/v1\nharness: codex\nsecretEnv: K\nconfig:\n  wireApi: off\n",
    )
    with pytest.raises(YamlBooleanTrapError, match=r"at config\.wireApi"):
        load_config(tmp_path / "x.yaml")


def test_quoted_yaml_booleans_pass_through_verbatim(tmp_path):
    _write_yaml(
        tmp_path,
        "x.yaml",
        "schema: agedum-provider/v1\n"
        'secretEnv: "on"\n'
        "config:\n"
        '  model: "yes"\n'
        "  extraEnv:\n"
        '    FOO: "off"\n',
    )
    config = load_config(tmp_path / "x.yaml")
    assert config["secretEnv"] == "on"
    assert config["config"]["model"] == "yes"
    assert config["config"]["extraEnv"]["FOO"] == "off"


def test_yaml_legitimate_booleans_load_untouched(tmp_path):
    _write_yaml(
        tmp_path,
        "x.yaml",
        "schema: agedum-provider/v1\n"
        "harness: claude\n"
        "abstract: true\n"
        "config:\n"
        "  foldSystemMessages: true\n"
        "  disableCaching: false\n",
    )
    config = load_config(tmp_path / "x.yaml")
    assert config["abstract"] is True
    assert config["config"]["foldSystemMessages"] is True
    assert config["config"]["disableCaching"] is False


# --- YAML values survive verbatim ---


def test_yaml_var_placeholders_stay_verbatim(tmp_path):
    # pyyaml does not interpolate ${VAR}; the placeholder must survive load untouched so
    # the per-harness translation sees exactly what a JSON config would carry.
    _write_yaml(tmp_path, "x.yaml", _sample_envelope_yaml())
    servers = load_config(tmp_path / "x.yaml")["config"]["mcpServers"]
    assert servers["nodum"]["env"]["NODUM_AGENT_TOKEN"] == "${NODUM_AGENT_TOKEN}"


# --- resolution: suffix-swap fallback ---


def test_resolve_no_suffix_prefers_json_when_both_exist(tmp_path):
    _write_config(tmp_path, "x.json", {"harness": "claude"})
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\n")
    assert resolve_config_path("x", tmp_path) == tmp_path / "x.json"


def test_resolve_no_suffix_falls_back_to_yaml(tmp_path):
    path = _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\n")
    assert resolve_config_path("x", tmp_path) == path


def test_resolve_no_suffix_falls_back_to_yml(tmp_path):
    # A .yml-only config is rostered under its stripped id, so `agedum x` must reach it.
    path = _write_yaml(tmp_path, "x.yml", "schema: agedum-provider/v1\n")
    assert resolve_config_path("x", tmp_path) == path


def test_resolve_explicit_json_falls_back_to_yaml_sibling(tmp_path):
    # The conversion enabler: a base renamed .json → .yaml keeps its old referrers.
    path = _write_yaml(tmp_path, "base/claude.yaml", "schema: agedum-provider/v1\n")
    assert resolve_config_path("base/claude.json", tmp_path) == path


def test_resolve_explicit_json_stays_when_the_file_exists(tmp_path):
    path = _write_config(tmp_path, "x.json", {"harness": "claude"})
    assert resolve_config_path("x.json", tmp_path) == path


def test_resolve_explicit_yaml_resolves_as_is(tmp_path):
    path = _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\n")
    assert resolve_config_path("x.yaml", tmp_path) == path
    assert resolve_config_path("x.yml", tmp_path) == tmp_path / "x.yml"


def test_resolve_missing_ref_keeps_the_json_name(tmp_path):
    # Neither spelling exists: the .json name is returned so the load error is the
    # conventional one (no silent "tried everything" message).
    assert resolve_config_path("nope", tmp_path) == tmp_path / "nope.json"


# --- extends across formats ---


def test_yaml_child_extends_json_base(tmp_path):
    _write_config(tmp_path, "base/c.json", {"abstract": True, "config": {"effortLevel": "max"}})
    _write_yaml(tmp_path, "child.yaml", "schema: agedum-provider/v1\nextends: base/c.json\n")
    merged = load_merged_config(tmp_path / "child.yaml", tmp_path)
    assert merged == {"config": {"effortLevel": "max"}}  # abstract not inherited


def test_json_child_extends_yaml_base(tmp_path):
    _write_yaml(
        tmp_path,
        "base/c.yaml",
        "schema: agedum-provider/v1\nabstract: true\nconfig:\n  effortLevel: max\n",
    )
    _write_config(tmp_path, "child.json", {"extends": "base/c.yaml"})
    merged = load_merged_config(tmp_path / "child.json", tmp_path)
    assert merged == {"config": {"effortLevel": "max"}}


def test_required_env_union_across_formats(tmp_path):
    _write_yaml(
        tmp_path,
        "base.yaml",
        "schema: agedum-provider/v1\nabstract: true\nrequiredEnv: [NODUM_AGENT_TOKEN]\n",
    )
    _write_config(tmp_path, "child.json", {"extends": "base.yaml", "requiredEnv": ["BUFFER_KEY"]})
    merged = load_merged_config(tmp_path / "child.json", tmp_path)
    assert merged["requiredEnv"] == ["NODUM_AGENT_TOKEN", "BUFFER_KEY"]


def test_cycle_detection_across_formats(tmp_path):
    _write_yaml(tmp_path, "a.yaml", "schema: agedum-provider/v1\nextends: b.json\n")
    _write_config(tmp_path, "b.json", {"extends": "a.yaml"})
    with pytest.raises(ProviderError, match="circular"):
        load_merged_config(tmp_path / "a.yaml", tmp_path)


def test_yaml_base_schema_is_checked_even_when_extended(tmp_path):
    _write_yaml(tmp_path, "base.yaml", "abstract: true\n")  # schema missing
    _write_config(tmp_path, "child.json", {"extends": "base.yaml"})
    with pytest.raises(ProviderSchemaError, match="agedum-provider/v1"):
        load_merged_config(tmp_path / "child.json", tmp_path)


def test_load_merged_config_reports_the_entry_format(tmp_path):
    _write_yaml(tmp_path, "base.yaml", "schema: agedum-provider/v1\nabstract: true\n")
    _write_config(tmp_path, "j-child.json", {"extends": "base.yaml"})
    _write_yaml(tmp_path, "y-child.yaml", "schema: agedum-provider/v1\nextends: base.yaml\n")
    # The reported format is the launched file's own, not its bases'.
    assert load_merged_config_with_format(tmp_path / "j-child.json", tmp_path).format == "json"
    assert load_merged_config_with_format(tmp_path / "y-child.yaml", tmp_path).format == "yaml"


# --- include fragments (composition, not inheritance) ---


def test_include_string_form_pastes_the_fragment(tmp_path):
    _write_config(
        tmp_path,
        "base/mcp.json",
        {"abstract": True, "requiredEnv": ["NODUM_AGENT_TOKEN"], "config": {"mcpServers": {}}},
    )
    child = _write_config(
        tmp_path,
        "claude/opus.json",
        {"include": "base/mcp.json", "harness": "claude", "config": {"model": "opus"}},
    )
    merged = load_merged_config(child, tmp_path)
    # Fragment pasted in; meta keys (abstract, include) stripped from the result.
    assert merged == {
        "requiredEnv": ["NODUM_AGENT_TOKEN"],
        "harness": "claude",
        "config": {"mcpServers": {}, "model": "opus"},
    }


def test_include_list_form_merges_left_to_right(tmp_path):
    _write_config(tmp_path, "a.json", {"config": {"x": 1, "y": 1}})
    _write_config(tmp_path, "b.json", {"config": {"y": 2, "z": 2}})
    child = _write_config(tmp_path, "child.json", {"include": ["a.json", "b.json"]})
    # Earlier include is the more default: b's y beats a's y, the child keeps both.
    assert load_merged_config(child, tmp_path)["config"] == {"x": 1, "y": 2, "z": 2}


def test_own_keys_beat_included_keys(tmp_path):
    _write_config(tmp_path, "frag.json", {"config": {"model": "fragment"}, "favorite": True})
    child = _write_config(
        tmp_path, "child.json", {"include": "frag.json", "config": {"model": "own"}}
    )
    merged = load_merged_config(child, tmp_path)
    assert merged["config"] == {"model": "own"}  # the file's own keys win
    assert merged["favorite"] is True  # …but everything not overridden still comes along


def test_extends_chain_beats_included_keys(tmp_path):
    # Inheritance overrides composition: a base's keys beat an included fragment's on
    # conflict, and the file's own keys beat both.
    _write_config(tmp_path, "frag.json", {"config": {"model": "from-include", "extra": 1}})
    _write_config(tmp_path, "base.json", {"config": {"model": "from-extends"}})
    child = _write_config(
        tmp_path,
        "child.json",
        {"include": "frag.json", "extends": "base.json", "config": {"model": "own"}},
    )
    assert load_merged_config(child, tmp_path)["config"] == {"model": "own", "extra": 1}


def test_required_env_unions_across_include_extends_and_own(tmp_path):
    _write_config(tmp_path, "frag.json", {"requiredEnv": ["FRAG_KEY"]})
    _write_config(tmp_path, "base.json", {"requiredEnv": ["BASE_KEY", "SHARED"]})
    child = _write_config(
        tmp_path,
        "child.json",
        {"include": "frag.json", "extends": "base.json", "requiredEnv": ["SHARED", "OWN_KEY"]},
    )
    # Include layer first, then the extends chain, then the file's own — deduped in order.
    assert load_merged_config(child, tmp_path)["requiredEnv"] == [
        "FRAG_KEY",
        "BASE_KEY",
        "SHARED",
        "OWN_KEY",
    ]


def test_include_is_recursive(tmp_path):
    _write_config(tmp_path, "deep.json", {"config": {"a": 1}})
    _write_config(tmp_path, "mid.json", {"include": "deep.json", "config": {"b": 2}})
    child = _write_config(tmp_path, "child.json", {"include": "mid.json", "config": {"c": 3}})
    assert load_merged_config(child, tmp_path)["config"] == {"a": 1, "b": 2, "c": 3}


def test_include_target_extends_chain_resolved_within_it(tmp_path):
    # A fragment's own extends is resolved before it is pasted: the composition carries the
    # fragment's *effective* config, not its raw body.
    _write_config(tmp_path, "grand.json", {"config": {"a": 1, "deep": True}})
    _write_config(tmp_path, "frag.json", {"extends": "grand.json", "config": {"b": 2}})
    child = _write_config(tmp_path, "child.json", {"include": "frag.json", "config": {"c": 3}})
    assert load_merged_config(child, tmp_path)["config"] == {"a": 1, "deep": True, "b": 2, "c": 3}


def test_diamond_include_merges_once_without_duplicate_error(tmp_path):
    # The same fragment reached through two paths is a DAG merge, not a cycle: it merges
    # twice but idempotently (deep-merge is idempotent, requiredEnv dedupes).
    _write_config(tmp_path, "shared.json", {"requiredEnv": ["K"], "config": {"shared": True}})
    _write_config(tmp_path, "left.json", {"include": "shared.json", "config": {"l": 1}})
    _write_config(tmp_path, "right.json", {"include": "shared.json", "config": {"r": 1}})
    child = _write_config(tmp_path, "child.json", {"include": ["left.json", "right.json"]})
    merged = load_merged_config(child, tmp_path)
    assert merged["requiredEnv"] == ["K"]
    assert merged["config"] == {"shared": True, "l": 1, "r": 1}


def test_include_cycle_errors(tmp_path):
    _write_config(tmp_path, "a.json", {"include": "b.json"})
    _write_config(tmp_path, "b.json", {"include": "a.json"})
    with pytest.raises(ProviderError, match="circular"):
        load_merged_config(tmp_path / "a.json", tmp_path)


def test_mixed_include_extends_cycle_errors(tmp_path):
    # Cycle detection spans the combined graph: a extends b, b includes a.
    _write_config(tmp_path, "a.json", {"extends": "b.json"})
    _write_config(tmp_path, "b.json", {"include": "a.json"})
    with pytest.raises(ProviderError, match="circular"):
        load_merged_config(tmp_path / "a.json", tmp_path)


def test_self_include_errors(tmp_path):
    child = _write_config(tmp_path, "a.json", {"include": "a.json"})
    with pytest.raises(ProviderError, match="circular"):
        load_merged_config(child, tmp_path)


def test_include_missing_target_errors(tmp_path):
    child = _write_config(tmp_path, "child.json", {"include": "nope.json"})
    with pytest.raises(ProviderError, match="cannot read"):
        load_merged_config(child, tmp_path)


def test_include_non_mapping_target_errors(tmp_path):
    _write_yaml(tmp_path, "frag.yaml", "- a\n- b\n")
    child = _write_config(tmp_path, "child.json", {"include": "frag.yaml"})
    with pytest.raises(ProviderError, match="YAML mapping"):
        load_merged_config(child, tmp_path)


def test_include_must_be_string_or_list(tmp_path):
    child = _write_config(tmp_path, "child.json", {"include": 5})
    with pytest.raises(ProviderError, match="include"):
        load_merged_config(child, tmp_path)


def test_include_key_never_appears_in_merged_dict(tmp_path):
    _write_config(tmp_path, "frag.json", {"config": {"a": 1}})
    child = _write_config(tmp_path, "child.json", {"include": ["frag.json", "frag.json"]})
    merged = load_merged_config(child, tmp_path)
    assert "include" not in merged
    assert "include" not in load_merged_config(tmp_path / "frag.json", tmp_path)


def test_boolean_trap_in_included_fragment_names_the_fragment_file(tmp_path):
    # The trap walk runs per-file at load, so the error names the fragment's own path —
    # not the including config's.
    _write_yaml(
        tmp_path,
        "frag.yaml",
        "schema: agedum-provider/v1\nconfig:\n  extraEnv:\n    FOO: no\n",
    )
    child = _write_config(tmp_path, "child.json", {"include": "frag.yaml"})
    with pytest.raises(YamlBooleanTrapError, match="frag.yaml.*config\\.extraEnv\\.FOO"):
        load_merged_config(child, tmp_path)


def test_abstract_include_target_is_a_fragment_not_a_launch(tmp_path):
    # `abstract` is not inherited through include: the composition is fine and the merged
    # result carries no abstract key; the fragment itself still refuses a direct launch
    # (that refusal reads the raw file — exercised at the CLI level).
    _write_config(
        tmp_path,
        "frag.json",
        {"abstract": True, "harness": "claude", "config": {"effortLevel": "max"}},
    )
    child = _write_config(tmp_path, "child.json", {"include": "frag.json"})
    merged = load_merged_config(child, tmp_path)
    assert merged == {"harness": "claude", "config": {"effortLevel": "max"}}
    # And the fragment stays out of --providers, exactly like an abstract base —
    # while the child including it is listed, its harness arriving through the include.
    (summary,) = list_providers(tmp_path)
    assert (summary.name, summary.harness) == ("child", "claude")


def test_json_config_includes_yaml_fragment_and_vice_versa(tmp_path):
    _write_yaml(tmp_path, "frag.yaml", "schema: agedum-provider/v1\nconfig:\n  effortLevel: max\n")
    json_child = _write_config(tmp_path, "j.json", {"include": "frag.yaml"})
    assert load_merged_config(json_child, tmp_path)["config"] == {"effortLevel": "max"}

    _write_config(tmp_path, "frag2.json", {"config": {"effortLevel": "max"}})
    yaml_child = _write_yaml(
        tmp_path, "y.yaml", "schema: agedum-provider/v1\ninclude: frag2.json\n"
    )
    assert load_merged_config(yaml_child, tmp_path)["config"] == {"effortLevel": "max"}


def test_include_ref_falls_back_to_yaml_sibling(tmp_path):
    # Include refs resolve by the same rule as extends: an explicit .json ref that only
    # exists as .yaml still resolves (the conversion enabler).
    _write_yaml(tmp_path, "base/mcp.yaml", "schema: agedum-provider/v1\nconfig:\n  m: 1\n")
    child = _write_config(tmp_path, "child.json", {"include": "base/mcp.json"})
    assert load_merged_config(child, tmp_path)["config"] == {"m": 1}


def test_yaml_boolean_trap_in_include_ref(tmp_path):
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\ninclude: on\n")
    with pytest.raises(YamlBooleanTrapError, match="at include"):
        load_config(tmp_path / "x.yaml")


# --- provider listing across formats ---


def test_list_providers_includes_yaml_configs(tmp_path):
    _write_config(tmp_path, "claude/ds.json", {"harness": "claude", "config": {"model": "m"}})
    _write_yaml(tmp_path, "claude/opus.yaml", "schema: agedum-provider/v1\nharness: claude\n")
    summaries = list_providers(tmp_path)
    assert [(s.name, s.harness, s.error) for s in summaries] == [
        ("claude/ds", "claude", None),
        ("claude/opus", "claude", None),
    ]


def test_list_providers_json_wins_for_a_shared_stem(tmp_path):
    # Both extensions for one id would collide in the roster; .json is the one listed.
    _write_config(tmp_path, "x.json", {"harness": "claude", "config": {"model": "json-m"}})
    _write_yaml(tmp_path, "x.yaml", "schema: agedum-provider/v1\nharness: kimi\n")
    (summary,) = list_providers(tmp_path)
    assert (summary.name, summary.harness, summary.model) == ("x", "claude", "json-m")


def test_list_providers_skips_abstract_yaml(tmp_path):
    _write_yaml(
        tmp_path,
        "base/c.yaml",
        "schema: agedum-provider/v1\nabstract: true\nconfig:\n  model: m\n",
    )
    assert list_providers(tmp_path) == []


def test_list_providers_reports_a_broken_yaml_as_an_error_row(tmp_path):
    _write_yaml(tmp_path, "bad.yaml", "schema: agedum-provider/v1\nharness: [claude\n")
    (summary,) = list_providers(tmp_path)
    assert summary.name == "bad"
    assert summary.error is not None and "invalid YAML" in summary.error


# --- model catalogue (models.yaml) + modelRef expansion ---


_CATALOGUE_YAML = """\
schema: agedum-models/v1
models:
  deepseek-v4-pro:
    name: DeepSeek V4 Pro
    limit:
      context: 1000000
      output: 65536
  deepseek-flash:
    name: DeepSeek V4.1 Flash
    attachment: true
    limit:
      context: 1000000
      output: 65536
    modalities:
      input: [text, image]
      output: [text]
"""


def _write_catalogue(root, text=_CATALOGUE_YAML, rel="models.yaml"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _oc_config(provider_def):
    """A minimal opencode config dict around one providerDef entry (or list)."""
    return {"harness": "opencode", "secretEnv": "K", "config": {"providerDef": provider_def}}


def test_load_model_catalog_happy_path(tmp_path):
    path = _write_catalogue(tmp_path)
    models = load_model_catalog(path)
    assert set(models) == {"deepseek-v4-pro", "deepseek-flash"}
    assert models["deepseek-v4-pro"] == {
        "name": "DeepSeek V4 Pro",
        "limit": {"context": 1000000, "output": 65536},
    }
    assert models["deepseek-flash"]["attachment"] is True
    assert models["deepseek-flash"]["modalities"] == {
        "input": ["text", "image"],
        "output": ["text"],
    }


def test_load_model_catalog_passes_unknown_keys_through_verbatim(tmp_path):
    # The catalogue is a verbatim fragment source: any key opencode consumes beyond
    # the validated vocabulary (variants, options, …) rides along untouched.
    _write_catalogue(
        tmp_path,
        "schema: agedum-models/v1\n"
        "models:\n"
        "  gpt-5.6-sol:\n"
        "    name: GPT-5.6 Sol\n"
        "    attachment: true\n"
        "    variants:\n"
        "      max:\n"
        "        disabled: true\n"
        "    options:\n"
        "      reasoningEffort: high\n",
    )
    (entry,) = load_model_catalog(tmp_path / "models.yaml").values()
    assert entry["variants"] == {"max": {"disabled": True}}
    assert entry["options"] == {"reasoningEffort": "high"}


@pytest.mark.parametrize(
    "text",
    ["schema: other/v1\nmodels: {}\n", "models: {}\n"],
)
def test_load_model_catalog_rejects_a_wrong_or_missing_schema(tmp_path, text):
    _write_catalogue(tmp_path, text)
    with pytest.raises(ModelCatalogSchemaError, match="agedum-models/v1"):
        load_model_catalog(tmp_path / "models.yaml")


@pytest.mark.parametrize(
    "text",
    ["schema: agedum-models/v1\n", "schema: agedum-models/v1\nmodels: [a]\n"],
)
def test_load_model_catalog_requires_a_models_mapping(tmp_path, text):
    _write_catalogue(tmp_path, text)
    with pytest.raises(ModelCatalogSchemaError, match="`models` must be a mapping"):
        load_model_catalog(tmp_path / "models.yaml")


def test_load_model_catalog_entry_must_be_a_mapping(tmp_path):
    _write_catalogue(tmp_path, "schema: agedum-models/v1\nmodels:\n  m1: nope\n")
    with pytest.raises(ModelCatalogSchemaError, match="entry for model 'm1' must be a mapping"):
        load_model_catalog(tmp_path / "models.yaml")


@pytest.mark.parametrize(
    ("entry", "key"),
    [
        ("  m1:\n    name: 5\n", "name"),
        ("  m1:\n    attachment: yes-please\n", "attachment"),
        ("  m1:\n    limit: big\n", "limit"),
        ("  m1:\n    limit:\n      context: big\n", "limit.context"),
        ("  m1:\n    limit:\n      context: true\n", "limit.context"),
        ("  m1:\n    modalities: text\n", "modalities"),
        ("  m1:\n    modalities:\n      input: text\n", "modalities.input"),
        ("  m1:\n    modalities:\n      input: [text, 3]\n", "modalities.input"),
    ],
)
def test_load_model_catalog_entry_type_errors_name_the_model_and_key(tmp_path, entry, key):
    _write_catalogue(tmp_path, f"schema: agedum-models/v1\nmodels:\n{entry}")
    with pytest.raises(ModelCatalogSchemaError, match=f"'m1' key `{key}`"):
        load_model_catalog(tmp_path / "models.yaml")


def test_load_model_catalog_accepts_null_limits(tmp_path):
    # A null limit is legitimate in the oc vocabulary (unset = opencode's default).
    _write_catalogue(
        tmp_path,
        "schema: agedum-models/v1\nmodels:\n  m1:\n    name: M1\n    limit:\n      context: null\n",
    )
    assert load_model_catalog(tmp_path / "models.yaml")["m1"]["limit"] == {"context": None}


def test_expand_model_refs_files_the_catalogue_entry_under_the_provider(tmp_path):
    _write_catalogue(tmp_path)
    config = _oc_config(
        {"id": "deepseek", "npm": "@ai-sdk/openai-compatible", "modelRef": "deepseek-v4-pro"}
    )
    expanded = expand_model_refs(config, tmp_path)
    entry = expanded["config"]["opencodeConfig"]["provider"]["deepseek"]["models"][
        "deepseek-v4-pro"
    ]
    assert entry == {"name": "DeepSeek V4 Pro", "limit": {"context": 1000000, "output": 65536}}
    assert "modelRef" not in expanded["config"]["providerDef"]
    assert "modelsCatalog" not in expanded


def test_expand_model_refs_build_launch_carries_the_fragment(tmp_path):
    # End to end through the launch: the expanded fragment reaches
    # OPENCODE_CONFIG_CONTENT exactly where the generated oc configs carry it.
    _write_catalogue(tmp_path)
    config = _oc_config(
        {
            "id": "deepseek",
            "npm": "@ai-sdk/openai-compatible",
            "baseUrl": "https://api.deepseek.com",
            "apiKeyEnv": "K",
            "modelRef": "deepseek-flash",
        }
    )
    expanded = expand_model_refs(config, tmp_path)
    launch = build_launch(expanded, {"K": "tok"})
    document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    models = document["provider"]["deepseek"]["models"]
    assert models["deepseek-flash"]["name"] == "DeepSeek V4.1 Flash"
    assert models["deepseek-flash"]["attachment"] is True


def test_expand_model_refs_accepts_a_list_of_refs(tmp_path):
    _write_catalogue(tmp_path)
    config = _oc_config({"id": "deepseek", "modelRef": ["deepseek-v4-pro", "deepseek-flash"]})
    expanded = expand_model_refs(config, tmp_path)
    models = expanded["config"]["opencodeConfig"]["provider"]["deepseek"]["models"]
    assert set(models) == {"deepseek-v4-pro", "deepseek-flash"}


def test_expand_model_refs_unknown_ref_names_ref_and_catalogue(tmp_path):
    _write_catalogue(tmp_path)
    config = _oc_config({"id": "deepseek", "modelRef": "no-such-model"})
    with pytest.raises(ProviderError, match="modelRef 'no-such-model'.*models.yaml"):
        expand_model_refs(config, tmp_path)


def test_expand_model_refs_absent_catalogue_names_the_path(tmp_path):
    config = _oc_config({"id": "deepseek", "modelRef": "deepseek-v4-pro"})
    with pytest.raises(ProviderError, match="no model catalogue at.*models.yaml"):
        expand_model_refs(config, tmp_path)


def test_expand_model_refs_is_opencode_only(tmp_path):
    _write_catalogue(tmp_path)
    config = {"harness": "claude", "config": {"providerDef": {"id": "x", "modelRef": "m"}}}
    with pytest.raises(ProviderError, match="only implemented for the opencode harness"):
        expand_model_refs(config, tmp_path)


def test_expand_model_refs_provider_def_without_id_errors(tmp_path):
    _write_catalogue(tmp_path)
    config = _oc_config({"npm": "@ai-sdk/openai-compatible", "modelRef": "deepseek-v4-pro"})
    with pytest.raises(ProviderError, match="needs an `id`"):
        expand_model_refs(config, tmp_path)


def test_expand_model_refs_without_refs_or_override_is_identity(tmp_path):
    # Zero behaviour change: no modelRef anywhere and no modelsCatalog — the config is
    # returned untouched and the catalogue is never even read (it does not exist here).
    config = _oc_config({"id": "deepseek", "npm": "@ai-sdk/openai-compatible"})
    assert expand_model_refs(config, tmp_path) == config


def test_expand_model_refs_models_catalog_override(tmp_path):
    # The default models.yaml does not exist; the override carries the entry. An
    # extensionless ref resolves through the ordinary ref rule (.yaml sibling).
    _write_catalogue(tmp_path, rel="catalogues/alt.yaml")
    config = {
        **_oc_config({"id": "deepseek", "modelRef": "deepseek-v4-pro"}),
        "modelsCatalog": "catalogues/alt",
    }
    expanded = expand_model_refs(config, tmp_path)
    models = expanded["config"]["opencodeConfig"]["provider"]["deepseek"]["models"]
    assert models["deepseek-v4-pro"]["name"] == "DeepSeek V4 Pro"
    assert "modelsCatalog" not in expanded


def test_expand_model_refs_models_catalog_rejects_a_non_yaml_ref(tmp_path):
    _write_config(tmp_path, "catalogues/alt.json", {"schema": "agedum-models/v1"})
    config = {
        **_oc_config({"id": "deepseek", "modelRef": "deepseek-v4-pro"}),
        "modelsCatalog": "catalogues/alt.json",
    }
    with pytest.raises(ProviderError, match=r"must resolve to a \.yaml catalogue"):
        expand_model_refs(config, tmp_path)


def test_expand_model_refs_declared_models_catalog_must_load_even_without_refs(tmp_path):
    # A declared-but-broken pointer must not be silent — the catalogue loads and
    # validates even when no modelRef references it.
    config = {**_oc_config({"id": "deepseek"}), "modelsCatalog": "absent.yaml"}
    with pytest.raises(ProviderError, match="no model catalogue at"):
        expand_model_refs(config, tmp_path)


def test_expand_model_refs_inline_entry_is_kept_and_wins_on_conflict(tmp_path):
    # An authored inline fragment for the same model is the more specific layer:
    # kept as-is, winning on conflict over the catalogue's values.
    _write_catalogue(tmp_path)
    config = {
        "harness": "opencode",
        "secretEnv": "K",
        "config": {
            "providerDef": {"id": "deepseek", "modelRef": "deepseek-v4-pro"},
            "opencodeConfig": {
                "provider": {
                    "deepseek": {
                        "models": {
                            "deepseek-v4-pro": {
                                "name": "Locally Renamed",
                                "options": {"reasoningEffort": "high"},
                            }
                        }
                    }
                }
            },
        },
    }
    expanded = expand_model_refs(config, tmp_path)
    entry = expanded["config"]["opencodeConfig"]["provider"]["deepseek"]["models"][
        "deepseek-v4-pro"
    ]
    assert entry == {
        "name": "Locally Renamed",
        "limit": {"context": 1000000, "output": 65536},
        "options": {"reasoningEffort": "high"},
    }


@pytest.mark.parametrize("dict_form", [True, False])
def test_expand_model_refs_parity_with_the_inline_catalog(tmp_path, dict_form):
    # The whole point: a modelRef config expands to exactly the dict the equivalent
    # hand-written config (catalog block inline in opencodeConfig, no modelRef)
    # merges to — for both the single-dict and the list providerDef form.
    _write_catalogue(tmp_path)
    catalog_entry = {
        "name": "DeepSeek V4 Pro",
        "limit": {"context": 1000000, "output": 65536},
    }
    provider_def = (
        {"id": "deepseek", "npm": "@ai-sdk/openai-compatible", "modelRef": "deepseek-v4-pro"}
        if dict_form
        else [{"id": "deepseek", "npm": "@ai-sdk/openai-compatible", "modelRef": "deepseek-v4-pro"}]
    )
    ref_config = _oc_config(provider_def)
    inline_provider_def = (
        {"id": "deepseek", "npm": "@ai-sdk/openai-compatible"}
        if dict_form
        else [{"id": "deepseek", "npm": "@ai-sdk/openai-compatible"}]
    )
    inline_config = {
        "harness": "opencode",
        "secretEnv": "K",
        "config": {
            "providerDef": inline_provider_def,
            "opencodeConfig": {
                "provider": {"deepseek": {"models": {"deepseek-v4-pro": catalog_entry}}}
            },
        },
    }
    assert expand_model_refs(ref_config, tmp_path) == inline_config


def test_expand_model_refs_through_the_yaml_pipeline(tmp_path):
    # End to end over the real load path: a YAML config (schema envelope) merged,
    # then expanded.
    _write_catalogue(tmp_path)
    _write_yaml(
        tmp_path,
        "oc/hand.yaml",
        "schema: agedum-provider/v1\n"
        "harness: opencode\n"
        "secretEnv: K\n"
        "config:\n"
        "  providerDef:\n"
        "    id: deepseek\n"
        "    modelRef: deepseek-v4-pro\n",
    )
    merged = load_merged_config_with_format(tmp_path / "oc/hand.yaml", tmp_path)
    expanded = expand_model_refs(merged.config, tmp_path)
    models = expanded["config"]["opencodeConfig"]["provider"]["deepseek"]["models"]
    assert models["deepseek-v4-pro"]["name"] == "DeepSeek V4 Pro"


def test_yaml_models_catalog_boolean_trap(tmp_path):
    _write_yaml(
        tmp_path,
        "oc/hand.yaml",
        "schema: agedum-provider/v1\nharness: opencode\nmodelsCatalog: on\n",
    )
    with pytest.raises(YamlBooleanTrapError, match="modelsCatalog"):
        load_config(tmp_path / "oc/hand.yaml")


def test_list_providers_skips_the_root_models_yaml(tmp_path):
    # The fixed catalogue is data, not a config — listing it would show a broken row
    # (it carries no provider schema).
    _write_catalogue(tmp_path)
    _write_config(tmp_path, "x.json", {"harness": "kimi", "config": {"model": "m"}})
    assert [s.name for s in list_providers(tmp_path)] == ["x"]


def test_list_providers_lists_a_subdirectory_models_yaml(tmp_path):
    # Only the exact root-level filename is excluded; a subdir models.yaml stays an
    # ordinary candidate (and shows as an error row — it has no provider schema).
    _write_catalogue(tmp_path, rel="sub/models.yaml")
    (summary,) = list_providers(tmp_path)
    assert summary.name == "sub/models"
    assert summary.error is not None


# --- YAML parity: kimi / pi / cline launchers through their real chains (child 4) ---
#
# Children 1–2 proved the YAML pipeline on envelope mechanics and the claude/codex
# families; this section pins the remaining live harnesses. Each real launcher is
# staged verbatim (see tests/yaml_fleet_fixtures.py) through its REAL extends chain in
# both spellings — the live JSON tree over the YAML sandbox root via the sibling
# fallback, and the all-YAML conversion shape — and both sides must merge to the same
# config and build the same Launch (argv, env, virtual files, warnings, sandbox).
# Harness-specific hotspots get their own assertions on the YAML-path artefacts,
# mirroring the JSON-path tests above.


def _assert_chain_parity(tmp_path, specs, label, env):
    """The YAML spelling of ``specs`` must merge and launch exactly like the JSON one."""
    json_child = fleet.write_chain(tmp_path / "json", specs(to_yaml=False))
    yaml_child = fleet.write_chain(tmp_path / "yaml", specs(to_yaml=True))
    json_merged = load_merged_config(json_child, tmp_path / "json")
    yaml_merged = load_merged_config(yaml_child, tmp_path / "yaml")
    assert json_merged == yaml_merged
    json_launch = build_launch(json_merged, env, label=label)
    yaml_launch = build_launch(yaml_merged, env, label=label)
    assert json_launch == yaml_launch
    return yaml_launch


def test_kimi_yaml_parity_through_the_real_chain(tmp_path):
    launch = _assert_chain_parity(
        tmp_path, fleet.kimi_specs, "kimi/kimi", fleet.LAUNCHER_ENV["kimi/kimi"]
    )
    # The seeded config.toml + mcp.json pair is the launcher's whole artefact surface.
    assert [entry[0].rsplit("/", 1)[1] for entry in launch.config_files] == [
        "config.toml",
        "mcp.json",
    ]


def test_pi_deepseek_yaml_parity_through_the_real_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.pi_specs, "deepseek"),
        "pi/deepseek",
        fleet.LAUNCHER_ENV["pi/deepseek"],
    )
    assert launch.warnings == ()  # no subagent routing, nothing to warn about


def test_pi_deepseek_flash_yaml_parity_through_the_real_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.pi_specs, "deepseek-flash"),
        "pi/deepseek-flash",
        fleet.LAUNCHER_ENV["pi/deepseek-flash"],
    )
    # subagentModel (plus the piSettings.subagents block) implicitly requires pi-subagents.
    assert any("pi-subagents" in warning for warning in launch.warnings)


def test_pi_flash_yaml_parity_through_the_real_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    launch = _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.pi_specs, "flash"),
        "pi/flash",
        fleet.LAUNCHER_ENV["pi/flash"],
    )
    assert launch.warnings == ()


def test_cline_deepseek_yaml_parity_through_the_real_chain(tmp_path):
    launch = _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.cline_specs, "deepseek"),
        "cline/deepseek",
        fleet.LAUNCHER_ENV["cline/deepseek"],
    )
    assert launch.config_files == ()  # named-provider path: flags only, nothing on disk


def test_cline_flash_yaml_parity_through_the_real_chain(tmp_path):
    _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.cline_specs, "flash"),
        "cline/flash",
        fleet.LAUNCHER_ENV["cline/flash"],
    )


def test_cline_kimi_code_auto_yaml_parity_through_the_real_chain(tmp_path):
    launch = _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.cline_specs, "kimi-code-auto"),
        "cline/kimi-code-auto",
        fleet.LAUNCHER_ENV["cline/kimi-code-auto"],
    )
    assert launch.config_files[0][0].endswith("/settings/providers.json")


def test_yaml_children_extending_json_bases(tmp_path, monkeypatch):
    # The reverse mixed chain: a converted YAML child may keep extending JSON bases —
    # explicit .json refs resolve as-is when the file exists. Each family's YAML child
    # over the real JSON bases must launch exactly like the live chain.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    root = tmp_path / "mixed"
    _write_config(root, "base/conception-sandbox.json", fleet.CONCEPTION_SANDBOX_JSON)
    _write_config(root, "base/pi-deepseek.json", fleet.PI_DEEPSEEK_BASE_JSON)
    _write_config(root, "base/cline-auto.json", fleet.CLINE_AUTO_BASE_JSON)
    _write_yaml(
        root,
        "kimi/kimi.yaml",
        fleet.KIMI_YAML.replace("base/conception-sandbox.yaml", "base/conception-sandbox.json"),
    )
    _write_yaml(
        root,
        "pi/flash.yaml",
        fleet.PI_CHILDREN_YAML["flash"].replace("base/pi-deepseek.yaml", "base/pi-deepseek.json"),
    )
    _write_yaml(
        root,
        "cline/kimi-code-auto.yaml",
        fleet.CLINE_CHILDREN_YAML["kimi-code-auto"].replace(
            "base/cline-auto.yaml", "base/cline-auto.json"
        ),
    )
    for child_rel, specs, label in (
        ("kimi/kimi.yaml", fleet.kimi_specs, "kimi/kimi"),
        ("pi/flash.yaml", functools.partial(fleet.pi_specs, "flash"), "pi/flash"),
        (
            "cline/kimi-code-auto.yaml",
            functools.partial(fleet.cline_specs, "kimi-code-auto"),
            "cline/kimi-code-auto",
        ),
    ):
        other = tmp_path / label.replace("/", "-")
        json_child = fleet.write_chain(other, specs(to_yaml=False))
        env = fleet.LAUNCHER_ENV[label]
        assert build_launch(
            load_merged_config(root / child_rel, root), env, label=label
        ) == build_launch(load_merged_config(json_child, other), env, label=label)


# --- kimi hotspots through YAML ---


def test_kimi_yaml_seeded_config_toml_and_models_map(tmp_path):
    # The seeded config.toml must carry the whole `models` map, the `[secondary_model]`
    # tier, the kimi providerType and the thinking effort — the identical artefact the
    # JSON path builds (mirrors test_kimi_code_subscription_uses_kimi_and_subscription_endpoint).
    child = fleet.write_chain(tmp_path, fleet.kimi_specs(to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path), fleet.LAUNCHER_ENV["kimi/kimi"], label="kimi/kimi"
    )
    assert launch.command == ["kimi", "--model", "k3", "--yolo"]  # binary + yolo hotspots
    target, content, merge_json, writable = launch.config_files[0]
    assert target == str(Path(launch.env["KIMI_CODE_HOME"]) / "config.toml")
    assert merge_json is False
    assert writable is True
    doc = tomllib.loads(content)
    assert doc["default_model"] == "k3"
    k3 = doc["models"]["k3"]
    assert k3["provider"] == "agedum"
    assert k3["model"] == "k3"
    assert k3["max_context_size"] == 1048576
    assert k3["capabilities"] == [
        "thinking",
        "always_thinking",
        "image_in",
        "video_in",
        "tool_use",
    ]
    # support_efforts is what keeps Kimi Code from collapsing the effort to plain `on`.
    assert k3["support_efforts"] == ["low", "high", "max"]
    assert k3["default_effort"] == "high"
    secondary = doc["models"]["kimi-for-coding"]
    assert secondary["max_context_size"] == 262144
    assert "support_efforts" not in secondary
    assert doc["secondary_model"] == {"model": "kimi-for-coding"}  # subagentModel hotspot
    assert doc["experimental"] == {"secondary-model": True}
    provider = doc["providers"]["agedum"]
    assert provider["type"] == "kimi"  # providerType hotspot
    assert provider["base_url"] == "https://api.kimi.com/coding/v1"
    assert provider["api_key"] == "sk-kimi-test"
    assert doc["thinking"] == {"enabled": True, "effort": "high"}  # thinking + effortLevel


def test_kimi_yaml_mcp_json_passthrough(tmp_path):
    # mcpServers is a separate generated mcp.json, passed through verbatim (no ${VAR}
    # rewriting — kimi rejects placeholders outright), seeded next to config.toml.
    child = fleet.write_chain(tmp_path, fleet.kimi_specs(to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path), fleet.LAUNCHER_ENV["kimi/kimi"], label="kimi/kimi"
    )
    target, content, merge_json, writable = launch.config_files[1]
    assert target == str(Path(launch.env["KIMI_CODE_HOME"]) / "mcp.json")
    assert merge_json is False
    assert writable is True
    assert json.loads(content) == {
        "mcpServers": {
            "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]},
            "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
        }
    }


def test_kimi_yaml_kimi_code_home_slug(tmp_path):
    # KIMI_CODE_HOME is derived from endpoint + model so repeat launches reuse the dir.
    child = fleet.write_chain(tmp_path, fleet.kimi_specs(to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path), fleet.LAUNCHER_ENV["kimi/kimi"], label="kimi/kimi"
    )
    assert launch.env["KIMI_CODE_HOME"] == str(
        Path.home() / ".cache" / "agedum" / "kimi" / "https-api-kimi-com-coding-v1-k3"
    )


# --- pi hotspots through YAML ---


def test_pi_deepseek_yaml_models_json_inputs_and_window(tmp_path, monkeypatch):
    # The generated models.json references the key by $ENV name and carries the child's
    # modelInputs + contextWindow on the model entry (mirrors
    # test_pi_custom_endpoint_generates_models_json).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    child = fleet.write_chain(tmp_path, fleet.pi_specs("deepseek", to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path),
        fleet.LAUNCHER_ENV["pi/deepseek"],
        label="pi/deepseek",
    )
    assert launch.command == ["pi", "--model", "agedum/deepseek-v4-pro", "--thinking", "high"]
    target, content, merge_json = launch.config_files[0]
    assert target == str(tmp_path / "pi-agent" / "models.json")
    assert merge_json is True  # augments the user's own models.json, never masks it
    provider = json.loads(content)["providers"]["agedum"]
    assert provider["baseUrl"] == "https://api.deepseek.com"  # inherited through the chain
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "$DEEPSEEK_API_KEY"  # by env-var name, never the value
    assert provider["models"] == [
        {"id": "deepseek-v4-pro", "input": ["text", "image"], "contextWindow": 1048576}
    ]


def test_pi_deepseek_flash_yaml_settings_deep_merge(tmp_path, monkeypatch):
    # subagentModel composes with piSettings into ONE settings.json fragment: the
    # baseline routes every builtin at agedum/<sub>, piSettings wins per agent — and the
    # deep-merge onto an existing user settings.json is identical from both paths
    # (mirrors test_pi_settings_composes_with_subagent_model).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    child = fleet.write_chain(tmp_path, fleet.pi_specs("deepseek-flash", to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path),
        fleet.LAUNCHER_ENV["pi/deepseek-flash"],
        label="pi/deepseek-flash",
    )
    assert launch.command == ["pi", "--model", "agedum/deepseek-v4-pro", "--thinking", "high"]
    target, content, merge_json = launch.config_files[1]
    assert target == str(tmp_path / "pi-agent" / "settings.json")
    assert merge_json is True
    overrides = json.loads(content)["subagents"]["agentOverrides"]
    expected = {
        name: {"model": "agedum/deepseek-v4-flash"}
        for name in (
            "scout",
            "researcher",
            "planner",
            "worker",
            "reviewer",
            "context-builder",
            "oracle",
            "delegate",
        )
    }
    expected.update(
        {
            "oracle": {"model": "agedum/deepseek-v4-flash", "thinking": "xhigh"},
            "planner": {"model": "agedum/deepseek-v4-flash", "thinking": "high"},
            "reviewer": {"model": "agedum/deepseek-v4-flash", "thinking": "high"},
            "scout": {"model": "agedum/deepseek-v4-flash", "thinking": "low"},
            "context-builder": {"model": "agedum/deepseek-v4-flash", "thinking": "low"},
        }
    )
    assert overrides == expected
    # The on-disk merge over a pre-existing user settings.json is the same document
    # whether the fragment came from YAML or JSON.
    user_file = tmp_path / "user-settings.json"
    user_file.write_text(json.dumps({"theme": "dark", "subagents": {"maxConcurrent": 3}}))
    json_launch = _assert_chain_parity(
        tmp_path,
        functools.partial(fleet.pi_specs, "deepseek-flash"),
        "pi/deepseek-flash",
        fleet.LAUNCHER_ENV["pi/deepseek-flash"],
    )
    yaml_merged_doc = merge_json_onto_file(user_file, content)
    user_file.write_text(json.dumps({"theme": "dark", "subagents": {"maxConcurrent": 3}}))
    json_merged_doc = merge_json_onto_file(user_file, json_launch.config_files[1][1])
    assert json.loads(yaml_merged_doc) == json.loads(json_merged_doc)
    assert json.loads(yaml_merged_doc)["theme"] == "dark"


def test_pi_yaml_require_extensions_warn_gate(tmp_path, monkeypatch):
    # requireExtensions (and `strict`) behave identically through the YAML load —
    # warnings never block, strict fails loudly (mirrors
    # test_pi_require_extensions_warns_when_missing / test_pi_strict_extensions_fails_loudly).
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))  # nothing installed
    _write_yaml(
        tmp_path,
        "pi/gated.yaml",
        "schema: agedum-provider/v1\n"
        "harness: pi\n"
        "config:\n"
        "  model: m\n"
        "  requireExtensions: [pi-intercom]\n",
    )
    yaml_launch = build_launch(load_config(tmp_path / "pi" / "gated.yaml"), {}, label="pi/gated")
    _write_config(
        tmp_path,
        "pi/gated.json",
        {"harness": "pi", "config": {"model": "m", "requireExtensions": ["pi-intercom"]}},
    )
    json_launch = build_launch(load_config(tmp_path / "pi" / "gated.json"), {}, label="pi/gated")
    assert yaml_launch == json_launch
    assert any(
        "pi-intercom" in warning and "not installed" in warning for warning in yaml_launch.warnings
    )
    _write_yaml(
        tmp_path,
        "pi/strict.yaml",
        "schema: agedum-provider/v1\n"
        "harness: pi\n"
        "config:\n"
        "  model: m\n"
        "  subagentModel: m-flash\n"
        "  strict: true\n",
    )
    with pytest.raises(ProviderError, match="pi-subagents.*not installed"):
        build_launch(load_config(tmp_path / "pi" / "strict.yaml"), {}, label="pi/strict")


# --- cline hotspots through YAML ---


def test_cline_yaml_key_env_derivation_and_key_flag(tmp_path):
    # Key-env naming: the base's secretEnv is derived into requiredEnv, exported to the
    # child, and rides `--key` in argv (masked in dry-run) — identical through YAML
    # (mirrors test_cline_appends_flags_and_passes_key).
    child = fleet.write_chain(tmp_path, fleet.cline_specs("deepseek", to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path),
        fleet.LAUNCHER_ENV["cline/deepseek"],
        label="cline/deepseek",
    )
    assert launch.env["DEEPSEEK_API_KEY"] == "sk-deepseek-test"
    assert "DEEPSEEK_API_KEY" in launch.secrets
    assert launch.command == [
        "cline",
        "--model",
        "deepseek-v4-pro",
        "--provider",
        "deepseek",
        "--thinking",
        "xhigh",
        "--key",
        "sk-deepseek-test",
    ]


def test_cline_yaml_auto_approve_and_compaction_off_enum(tmp_path):
    # autoApprove/compaction inherited from base/cline-auto keep their flags through the
    # YAML chain; and the `off` enum — the exact YAML 1.1 trap — quoted, still reaches
    # cline as `--compaction off` identically to the JSON spelling.
    child = fleet.write_chain(tmp_path, fleet.cline_specs("kimi-code-auto", to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path),
        fleet.LAUNCHER_ENV["cline/kimi-code-auto"],
        label="cline/kimi-code-auto",
    )
    assert launch.command[:5] == ["cline", "--compaction", "agentic", "--auto-approve", "true"]
    _write_yaml(tmp_path, "base/cline-auto.yaml", fleet.CLINE_AUTO_BASE_YAML)
    off_yaml = _write_yaml(
        tmp_path,
        "cline/off.yaml",
        "schema: agedum-provider/v1\n"
        "extends: base/cline-auto.yaml\n"
        "secretEnv: KIMI_API_KEY\n"
        "config:\n"
        '  compaction: "off"\n',
    )
    _write_config(
        tmp_path,
        "cline/off.json",
        {
            "extends": "base/cline-auto.yaml",
            "secretEnv": "KIMI_API_KEY",
            "config": {"compaction": "off"},
        },
    )
    env = {"KIMI_API_KEY": "sk-kimi-test"}
    off_launch = build_launch(load_merged_config(off_yaml, tmp_path), env, label="cline/off")
    json_launch = build_launch(
        load_merged_config(tmp_path / "cline" / "off.json", tmp_path), env, label="cline/off"
    )
    assert off_launch == json_launch
    # The base's autoApprove still rides along; the quoted `off` reaches cline verbatim.
    assert off_launch.command == [
        "cline",
        "--compaction",
        "off",
        "--auto-approve",
        "true",
        "--key",
        "sk-kimi-test",
    ]


def test_cline_yaml_context_window_and_max_tokens_models_array(tmp_path):
    # contextWindow/maxTokens teach cline's catalogue-less provider the window and output
    # cap via a one-entry models[]; the key is never written to disk (mirrors
    # test_cline_base_url_context_window_becomes_models_array).
    child = fleet.write_chain(tmp_path, fleet.cline_specs("kimi-code-auto", to_yaml=True))
    launch = build_launch(
        load_merged_config(child, tmp_path),
        fleet.LAUNCHER_ENV["cline/kimi-code-auto"],
        label="cline/kimi-code-auto",
    )
    assert launch.env["CLINE_DATA_DIR"] == str(
        Path.home() / ".cache" / "agedum" / "cline" / "https-api-kimi-com-coding-v1-kimi-for-coding"
    )
    target, content, merge_json, writable = launch.config_files[0]
    assert target == f"{launch.env['CLINE_DATA_DIR']}/settings/providers.json"
    assert merge_json is False
    assert writable is True
    doc = json.loads(content)
    assert doc["lastUsedProvider"] == "openai-compatible"
    settings = doc["providers"]["openai-compatible"]["settings"]
    assert settings["baseUrl"] == "https://api.kimi.com/coding/v1"
    assert settings["model"] == "kimi-for-coding"
    assert settings["apiKey"] == ""  # rides --key; nothing secret on disk
    assert settings["models"] == [
        {"id": "kimi-for-coding", "contextWindow": 262144, "maxTokens": 32768}
    ]
