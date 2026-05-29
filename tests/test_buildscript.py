import json

import pytest

from agedum.buildscript import BuildScriptError, build_script


def test_claude_full_mapping():
    script = build_script(
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
        }
    )
    assert "set -euo pipefail" in script
    assert 'export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:?' in script  # validated + exported
    assert "export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic" in script
    assert 'export ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY"' in script
    assert "unset ANTHROPIC_API_KEY" in script
    assert "export ANTHROPIC_MODEL=deepseek-v4-pro" in script
    assert "export ANTHROPIC_SMALL_FAST_MODEL=deepseek-v4-flash" in script
    assert "export ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash" in script
    assert "export CLAUDE_CODE_SUBAGENT_MODEL=deepseek-v4-pro" in script
    assert "export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000" in script
    assert "export CLAUDE_CODE_EFFORT_LEVEL=max" in script
    assert "export CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in script
    assert "export DISABLE_TELEMETRY=1" in script
    assert "DISABLE_PROMPT_CACHING" not in script  # false -> omitted
    assert "unset CLAUDE_CODE_USE_BEDROCK" in script
    assert script.rstrip().endswith('exec agedum --wrapper claude -- claude "$@"')


def test_claude_fold_system_messages_flag():
    script = build_script(
        {
            "harness": "claude",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {
                "baseUrl": "https://api.deepseek.com/anthropic",
                "model": "deepseek-v4-pro",
                "foldSystemMessages": True,
            },
        }
    )
    assert "export AGEDUM_FOLD_SYSTEM_MESSAGES=1" in script
    # the upstream URL stays the real endpoint; the proxy is interposed at run time
    assert "export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic" in script


def test_claude_fold_system_messages_omitted_when_unset():
    script = build_script(
        {
            "harness": "claude",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"baseUrl": "https://api.deepseek.com/anthropic", "model": "m"},
        }
    )
    assert "AGEDUM_FOLD_SYSTEM_MESSAGES" not in script


def test_claude_apikey_auth_style():
    script = build_script(
        {
            "harness": "claude",
            "secretEnv": "SOME_KEY",
            "config": {"baseUrl": "https://x/anthropic", "authStyle": "apikey", "model": "m"},
        }
    )
    assert 'export ANTHROPIC_API_KEY="$SOME_KEY"' in script
    assert "unset ANTHROPIC_AUTH_TOKEN" in script


def test_claude_native_runs_bare():
    script = build_script(
        {
            "harness": "claude",
            "slug": "claude-native",
            "config": {"baseUrl": "", "model": "", "maxContextTokens": 0, "disable1M": False},
        }
    )
    assert "ANTHROPIC_BASE_URL" not in script
    assert "env_file" not in script  # no requiredEnv -> no env block
    assert script.rstrip().endswith('exec agedum --wrapper claude -- claude "$@"')


def test_claude_baseurl_without_secret_errors():
    with pytest.raises(BuildScriptError, match="secretEnv"):
        build_script({"harness": "claude", "config": {"baseUrl": "https://x/anthropic"}})


def test_kimi_appends_flags():
    script = build_script(
        {
            "harness": "kimi",
            "secretEnv": "KIMI_API_KEY",
            "config": {"model": "kimi-k2.6", "thinking": True, "plan": True},
        }
    )
    assert 'export KIMI_API_KEY="${KIMI_API_KEY:?' in script
    expected = 'exec agedum --wrapper kimi -- kimi --model kimi-k2.6 --thinking --plan "$@"'
    assert script.rstrip().endswith(expected)


def test_kimi_no_thinking_flag():
    script = build_script({"harness": "kimi", "config": {"thinking": False}})
    assert "kimi --no-thinking" in script


def test_kimi_native_empty_config():
    script = build_script({"harness": "kimi", "config": {}})
    assert script.rstrip().endswith('exec agedum --wrapper kimi -- kimi "$@"')


def test_opencode_agent_options():
    script = build_script(
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
        }
    )
    assert "export OPENCODE_DISABLE_EXTERNAL_SKILLS=1" in script
    line = next(line for line in script.splitlines() if "OPENCODE_CONFIG_CONTENT" in line)
    payload = json.loads(line.split("=", 1)[1].strip().strip("'"))
    assert payload["model"] == "deepseek/deepseek-v4-flash"
    assert payload["agent"]["build"]["model"] == "deepseek/deepseek-v4-pro"
    # custom agent flagged primary -> mode; built-in "build" never gets mode
    assert payload["agent"]["high"]["mode"] == "primary"
    assert "mode" not in payload["agent"]["build"]
    assert payload["agent"]["high"]["options"]["reasoningEffort"] == "high"
    assert script.rstrip().endswith('exec agedum --wrapper opencode -- opencode "$@"')


def test_opencode_flat_effort_alias():
    script = build_script(
        {
            "harness": "opencode",
            "config": {"model": "deepseek/deepseek-v4-flash", "effortLevel": "low"},
        }
    )
    line = next(line for line in script.splitlines() if "OPENCODE_CONFIG_CONTENT" in line)
    payload = json.loads(line.split("=", 1)[1].strip().strip("'"))
    options = payload["provider"]["deepseek"]["models"]["deepseek-v4-flash"]["options"]
    assert options["reasoningEffort"] == "low"


def test_opencode_explicit_options_win_over_flat_effort():
    script = build_script(
        {
            "harness": "opencode",
            "config": {
                "model": "p/m",
                "effortLevel": "low",
                "defaultOptions": {"reasoningEffort": "high"},
            },
        }
    )
    line = next(line for line in script.splitlines() if "OPENCODE_CONFIG_CONTENT" in line)
    payload = json.loads(line.split("=", 1)[1].strip().strip("'"))
    assert payload["provider"]["p"]["models"]["m"]["options"]["reasoningEffort"] == "high"


def test_required_env_list_plus_secret():
    script = build_script(
        {
            "harness": "opencode",
            "secretEnv": "DEEPSEEK_API_KEY",
            "requiredEnv": ["DEEPSEEK_API_KEY", "OPENROUTER_KEY"],
            "config": {"model": "deepseek/deepseek-v4-flash"},
        }
    )
    assert 'export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:?' in script
    assert 'export OPENROUTER_KEY="${OPENROUTER_KEY:?' in script


def test_unknown_harness_errors():
    with pytest.raises(BuildScriptError, match="harness"):
        build_script({"harness": "agentsconf", "config": {"binary": "claude.sh"}})


def test_generation_is_deterministic():
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
    assert build_script(config) == build_script(json.loads(json.dumps(config)))


def test_extra_config_json_merges():
    script = build_script(
        {
            "harness": "opencode",
            "config": {"model": "p/m", "extraConfigJson": '{"theme": "tokyonight"}'},
        }
    )
    line = next(line for line in script.splitlines() if "OPENCODE_CONFIG_CONTENT" in line)
    payload = json.loads(line.split("=", 1)[1].strip().strip("'"))
    assert payload["theme"] == "tokyonight"
    assert payload["model"] == "p/m"
