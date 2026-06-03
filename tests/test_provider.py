import json

import pytest

from agedum.provider import (
    Launch,
    ProviderError,
    build_launch,
    default_env_file,
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
    # The token rides the required-env export (reasonix reads it via api_key_env) and is masked.
    assert launch.env["DEEPSEEK_API_KEY"] == "sk-rx-abc"
    assert "DEEPSEEK_API_KEY" in launch.secrets


def test_reasonix_bare_runs_chat():
    # No model: bare `reasonix chat` (uses the config default_model). No env beyond the key.
    launch = build_launch({"harness": "reasonix", "config": {}}, base_env={})
    assert launch.command == ["reasonix", "chat"]
    assert launch.env == {}


def test_reasonix_base_url_is_rejected():
    # reasonix has no base-URL flag/env; a custom endpoint is a [[providers]] block selected
    # by name, so a baseUrl would silently miss — reject it rather than launch wrong.
    with pytest.raises(ProviderError, match="reasonix has no base-URL flag"):
        build_launch(
            {
                "harness": "reasonix",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://api.deepseek.com", "model": "deepseek-pro"},
            },
            base_env={"DEEPSEEK_API_KEY": "tok"},
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
