import json

import pytest

from agedum.provider import (
    Launch,
    ProviderError,
    build_launch,
    default_env_file,
    list_providers,
    load_config,
    parse_env_file,
    providers_dir,
    required_env,
    resolve_config_path,
    with_prompt,
)

# --- config-path resolution ---


def test_resolve_name_under_providers_dir(tmp_path):
    assert resolve_config_path("ds-auto", tmp_path) == tmp_path / "ds-auto.json"


def test_resolve_explicit_json_path_is_verbatim():
    assert resolve_config_path("./local/x.json").name == "x.json"
    assert str(resolve_config_path("sub/dir/y.json")).endswith("sub/dir/y.json")


def test_resolve_absolute_path():
    assert str(resolve_config_path("/abs/p.json")) == "/abs/p.json"


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


# --- kimi env/command mapping ---


def test_kimi_appends_flags_and_exports_token():
    launch = build_launch(
        {
            "harness": "kimi",
            "secretEnv": "KIMI_API_KEY",
            "config": {"model": "kimi-k2.6", "thinking": True, "plan": True},
        },
        base_env={"KIMI_API_KEY": "kk"},
    )
    assert launch.env["KIMI_API_KEY"] == "kk"  # token reaches the child via required-env
    assert launch.command == ["kimi", "--model", "kimi-k2.6", "--thinking", "--plan"]


def test_kimi_no_thinking_flag():
    launch = build_launch({"harness": "kimi", "config": {"thinking": False}}, base_env={})
    assert launch.command == ["kimi", "--no-thinking"]


def test_kimi_native_empty_config():
    launch = build_launch({"harness": "kimi", "config": {}}, base_env={})
    assert launch.command == ["kimi"]
    assert launch.env == {}


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


def test_cline_base_url_is_rejected():
    # Cline has no base-URL flag; a baseUrl would launch against the wrong endpoint
    # silently, so it is rejected rather than ignored.
    with pytest.raises(ProviderError, match="cline has no base-URL flag"):
        build_launch(
            {
                "harness": "cline",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://api.deepseek.com/anthropic", "model": "m"},
            },
            base_env={"DEEPSEEK_API_KEY": "tok"},
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


def test_with_prompt_kimi_interactive_uses_prompt_flag():
    cmd = with_prompt(_launch("kimi", ["kimi", "--model", "k"]), [], "hi", interactive=True)
    assert cmd == ["kimi", "--model", "k", "--prompt", "hi"]


def test_with_prompt_kimi_run_appends_print():
    # kimi: --prompt seeds; --print makes the invocation non-interactive.
    assert with_prompt(_launch("kimi", ["kimi"]), [], "hi", interactive=False) == [
        "kimi",
        "--prompt",
        "hi",
        "--print",
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
