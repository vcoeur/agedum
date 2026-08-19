import json
import tomllib
from pathlib import Path

import pytest

from agedum.provider import (
    Launch,
    ProviderError,
    build_launch,
    default_env_file,
    list_providers,
    load_config,
    load_merged_config,
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
