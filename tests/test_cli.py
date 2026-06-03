import json
from pathlib import Path

import pytest

from agedum import __version__
from agedum.cli import main as cli


def _capture_run(monkeypatch):
    captured = {}

    def fake_run(root, plan, command, *, close_stdin=False):
        captured["command"] = command
        captured["plan"] = plan
        captured["close_stdin"] = close_stdin
        return 0

    monkeypatch.setattr(cli, "run_virtualfs", fake_run)
    return captured


def _hermetic_sources(monkeypatch):
    """Point the source loaders at a fake non-empty project so ``--dry-run`` compiles
    deterministically instead of reading the host's real project/global sources."""
    project = cli.Source(root=Path("/proj"), agents_md=Path("/proj/AGENTS.md"), skills_dir=None)
    monkeypatch.setattr(cli, "load_source", lambda: project)
    monkeypatch.setattr(cli, "load_global_source", lambda: cli.Source(Path("/g"), None, None))


def _no_launch(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not launch")

    monkeypatch.setattr(cli, "run_virtualfs", boom)


def test_version(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agedum", "--version"])
    cli.app()
    assert __version__ in capsys.readouterr().out


def test_help_when_no_args(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agedum"])
    cli.app()
    out = capsys.readouterr().out
    assert "usage: agedum" in out
    assert "--wrapper" in out
    assert "provider" in out.lower()
    assert "--build-script" not in out


# --- wrapper mode ---


def test_wrapper_passes_everything_after_dashdash(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr(
        "sys.argv", ["agedum", "--wrapper", "claude", "--", "claude", "--model", "x", "-p"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "--model", "x", "-p"]


def test_wrapper_equals_form(monkeypatch):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper=kimi", "--", "kimi", "-p", "hi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "hi"]


def test_wrapper_cline_is_registered(monkeypatch):
    # cline is a registered wrapper harness; dispatch reaches its compiler and runs.
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "cline", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "cline", "--", "cline", "task"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["cline", "task"]


def test_removed_legacy_alias_is_not_wrapper_mode(monkeypatch):
    # The `--claude`/`--kimi`/`--opencode` aliases were removed; the bare flag is no
    # longer wrapper mode and is rejected as an unknown provider option.
    monkeypatch.setattr("sys.argv", ["agedum", "--claude", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_wrapper_dry_run_lists_virtual_files_without_launching(monkeypatch, capsys):
    _hermetic_sources(monkeypatch)
    _no_launch(monkeypatch)
    target = Path.home() / ".claude" / "CLAUDE.md"
    plan = cli.Plan(binds=[(Path("/tmp/c/CLAUDE.md"), target)], origins={target: "/proj/AGENTS.md"})
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: plan)
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "claude", "--dry-run", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "project scope" in out
    assert "/proj/AGENTS.md" in out and "→ ~/.claude/CLAUDE.md" in out  # source disposition
    assert "command" in out


def test_dry_run_notes_empty_project_scope(monkeypatch, capsys):
    # A scope that contributes nothing is stated explicitly, not silently omitted.
    monkeypatch.setattr(cli, "load_source", lambda: cli.Source(Path("/proj"), None, None))
    monkeypatch.setattr(
        cli, "load_global_source", lambda: cli.Source(Path("/g"), Path("/g/AGENTS.md"), None)
    )
    _no_launch(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "claude", "--dry-run", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "(no AGENTS.md or .agents/skills found here)" in out


def test_wrapper_without_harness_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "--", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_wrapper_missing_dashdash_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--wrapper", "claude", "claude"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


# --- provider mode ---


def _write_provider(tmp_path, name, config, monkeypatch):
    providers = tmp_path / "providers"
    providers.mkdir(exist_ok=True)
    (providers / f"{name}.json").write_text(json.dumps(config))
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    # hermetic env file (empty) so the host's real ~/.config/agents/.env is never read
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))


def test_provider_by_name_launches(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    _write_provider(tmp_path, "mykimi", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "mykimi", "-p", "hi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "hi"]


def test_provider_by_path_launches(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    conf = tmp_path / "custom.json"
    conf.write_text(json.dumps({"harness": "kimi", "config": {"model": "k"}}))
    monkeypatch.setenv("AGENTS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr("sys.argv", ["agedum", str(conf)])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "--model", "k"]


def test_harness_args_after_provider_passthrough(monkeypatch, tmp_path):
    # A token after the provider that is not an agedum flag goes to the harness.
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    _write_provider(tmp_path, "mykimi", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "mykimi", "-p", "hi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "hi"]


def test_dry_run_after_provider_does_not_launch(monkeypatch, tmp_path, capsys):
    # The documented `agedum <provider> --dry-run`: --dry-run is agedum's even after
    # the provider, so it prints the plan instead of being passed to the harness.
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "ds.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://x/anthropic", "model": "m"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=tok\n")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    _no_launch(monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "ds", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert "ANTHROPIC_BASE_URL" in capsys.readouterr().out


def test_env_flag_after_provider(monkeypatch, tmp_path, capsys):
    # --env is also recognised after the provider name.
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "ds.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://x/anthropic", "model": "m"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / "alt.env"
    env_file.write_text("DEEPSEEK_API_KEY=tok\n")
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    _no_launch(monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "ds", "--env", str(env_file), "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert "ANTHROPIC_BASE_URL" in capsys.readouterr().out


def test_dashdash_forwards_literal_flag_to_harness(monkeypatch, tmp_path):
    # `--` after the provider is the escape hatch: --dry-run past it reaches the harness
    # verbatim rather than being claimed by agedum.
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda project, global_, dest: cli.Plan())
    _write_provider(tmp_path, "mykimi", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "mykimi", "--", "-p", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["kimi", "-p", "--dry-run"]


def test_provider_dry_run_masks_secrets(monkeypatch, tmp_path, capsys):
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "ds.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "slug": "claude-deepseek-auto",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://api.deepseek.com/anthropic", "model": "m"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-supersecret\n")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))

    # run_virtualfs must NOT be called in dry-run
    def boom(*a, **k):
        raise AssertionError("dry-run must not launch")

    monkeypatch.setattr(cli, "run_virtualfs", boom)
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "ds"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_BASE_URL" in out and "https://api.deepseek.com/anthropic" in out
    assert "sk-supersecret" not in out  # secret value masked
    assert "***" in out
    assert "unset ANTHROPIC_API_KEY" in out
    assert "command" in out
    assert "project scope" in out and "global scope" in out


def test_provider_dry_run_redacts_key_in_opencode_config(monkeypatch, tmp_path, capsys):
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "oc.json").write_text(
        json.dumps(
            {
                "harness": "opencode",
                "slug": "opencode-kimi",
                "requiredEnv": ["OPENROUTER_API_KEY"],
                "config": {
                    "model": "openrouter/moonshotai/kimi-k2.6",
                    "providerDef": {
                        "id": "openrouter",
                        "npm": "@openrouter/ai-sdk-provider",
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "apiKeyEnv": "OPENROUTER_API_KEY",
                    },
                },
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-or-supersecret\n")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))

    def boom(*a, **k):
        raise AssertionError("dry-run must not launch")

    monkeypatch.setattr(cli, "run_virtualfs", boom)
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "opencode", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "oc"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # The key baked into options.apiKey is redacted inside the pretty-printed config...
    assert "sk-or-supersecret" not in out
    assert "***" in out
    # ...while the non-secret provider details stay visible.
    assert "https://openrouter.ai/api/v1" in out
    assert "@openrouter/ai-sdk-provider" in out
    assert "openrouter/moonshotai/kimi-k2.6" in out


def test_provider_dry_run_with_explicit_env_flag(monkeypatch, tmp_path, capsys):
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "ds.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "secretEnv": "DEEPSEEK_API_KEY",
                "config": {"baseUrl": "https://x/anthropic", "model": "m"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / "alt.env"
    env_file.write_text("DEEPSEEK_API_KEY=tok\n")
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda project, global_, dest: cli.Plan())
    monkeypatch.setattr("sys.argv", ["agedum", "--env", str(env_file), "--dry-run", "ds"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_BASE_URL" in out and "https://x/anthropic" in out


def test_provider_dry_run_shows_dispositions_and_native_read(monkeypatch, tmp_path, capsys):
    # Realistic kimi sources + plan: project AGENTS.md read natively, project/global skills
    # bound, global AGENTS.md routed to the --agent-file.
    project = cli.Source(Path("/proj"), Path("/proj/AGENTS.md"), Path("/proj/.agents/skills"))
    global_ = cli.Source(Path("/g"), Path("/g/AGENTS.md"), Path("/g/.agents/skills"))
    monkeypatch.setattr(cli, "load_source", lambda: project)
    monkeypatch.setattr(cli, "load_global_source", lambda: global_)
    _no_launch(monkeypatch)
    p_skills, g_skills, agent_file = (
        Path("/proj/.kimi/skills"),
        Path("/g/.kimi/skills"),
        Path("/tmp/k/agent.yaml"),
    )
    plan = cli.Plan(
        binds=[(Path("/tmp/ps"), p_skills), (Path("/tmp/gs"), g_skills)],
        extra_args=["--agent-file", str(agent_file)],
        origins={
            p_skills: "/proj/.agents/skills",
            g_skills: "/g/.agents/skills",
            agent_file: "/g/AGENTS.md",
        },
        native_reads=[Path("/proj/AGENTS.md")],
    )
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda p, g, d: plan)
    _write_provider(tmp_path, "mykimi", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "mykimi"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "project scope" in out and "global scope" in out
    assert "read in place" in out  # project AGENTS.md read natively, not invisible
    assert "→ /proj/.kimi/skills" in out  # project skills injected
    assert "→ kimi agent file (passed via --agent-file)" in out  # global AGENTS.md
    assert "+ agedum appends: --agent-file /tmp/k/agent.yaml" in out


def test_provider_dry_run_pretty_prints_opencode_config(monkeypatch, tmp_path, capsys):
    _hermetic_sources(monkeypatch)
    _no_launch(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "opencode", lambda project, global_, dest: cli.Plan())
    _write_provider(
        tmp_path,
        "oc",
        {
            "harness": "opencode",
            "config": {"model": "deepseek/deepseek-v4-pro", "effortLevel": "high"},
        },
        monkeypatch,
    )
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "oc"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # OPENCODE_CONFIG_CONTENT is pretty-printed (indented), not a one-line blob.
    assert "OPENCODE_CONFIG_CONTENT\n" in out
    assert '"model": "deepseek/deepseek-v4-pro"' in out
    assert '"reasoningEffort": "high"' in out


def test_provider_missing_required_env_returns_1(monkeypatch, tmp_path):
    _write_provider(
        tmp_path,
        "ds",
        {
            "harness": "claude",
            "secretEnv": "DEEPSEEK_API_KEY",
            "config": {"baseUrl": "https://x/anthropic", "model": "m"},
        },
        monkeypatch,
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["agedum", "ds"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 1


def test_provider_unknown_name_returns_1(monkeypatch, tmp_path):
    _write_provider(tmp_path, "exists", {"harness": "kimi", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "nope"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 1


def test_provider_required_when_no_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_provider_unknown_option_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agedum", "--bogus", "x"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


# --- --prompt / --run ---


def test_prompt_flag_claude_interactive_positional(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--prompt", "hello there"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "hello there"]
    # --prompt is interactive: the harness keeps the inherited stdin.
    assert captured["close_stdin"] is False


def test_run_flag_claude_non_interactive(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--run", "do the thing"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "--print", "do the thing"]
    # --run is non-interactive: the harness gets /dev/null stdin so it cannot block.
    assert captured["close_stdin"] is True


def test_run_flag_kimi_appends_print(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "kimi", lambda p, g, d: cli.Plan())
    config = {"harness": "kimi", "config": {"model": "kimi-k2.6"}}
    _write_provider(tmp_path, "k", config, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "k", "--run", "summarise"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == [
        "kimi",
        "--model",
        "kimi-k2.6",
        "--prompt",
        "summarise",
        "--print",
    ]


def test_run_flag_opencode_uses_run_subcommand(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "opencode", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "oc", {"harness": "opencode", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "oc", "--run", "explain this"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["opencode", "run", "explain this"]
    # opencode `run` blocks forever on an open stdin — --run must close it.
    assert captured["close_stdin"] is True


def test_prompt_flag_recognised_before_provider(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "--prompt", "hi", "native"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "hi"]


def test_run_equals_form(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--run=ship it"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "--print", "ship it"]


def test_run_keeps_harness_passthrough_args(monkeypatch, tmp_path):
    # Passthrough harness args precede the seeded mode flags + prompt text.
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--add-dir", "/x", "--run", "go"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude", "--add-dir", "/x", "--print", "go"]


def test_prompt_and_run_mutually_exclusive(monkeypatch, tmp_path):
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--prompt", "a", "--run", "b"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_run_requires_a_value(monkeypatch, tmp_path):
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 2


def test_run_shows_in_dry_run_command(monkeypatch, tmp_path, capsys):
    _hermetic_sources(monkeypatch)
    _no_launch(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native", "--run", "do x", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--print" in out and "do x" in out


def test_bare_launch_keeps_stdin(monkeypatch, tmp_path):
    # No --prompt/--run: the harness launches interactively and inherits stdin.
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "claude", lambda p, g, d: cli.Plan())
    _write_provider(tmp_path, "native", {"harness": "claude", "config": {}}, monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "native"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["claude"]
    assert captured["close_stdin"] is False


# --- cline provider mode ---


def test_provider_cline_launches(monkeypatch, tmp_path):
    # `agedum <provider>` now accepts a cline harness and builds its flag command.
    captured = _capture_run(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "cline", lambda project, global_, dest: cli.Plan())
    _write_provider(
        tmp_path,
        "cl",
        {"harness": "cline", "config": {"model": "claude-opus-4-8", "provider": "anthropic"}},
        monkeypatch,
    )
    monkeypatch.setattr("sys.argv", ["agedum", "cl"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    assert captured["command"] == ["cline", "--model", "claude-opus-4-8", "--provider", "anthropic"]
    assert captured["close_stdin"] is False


def test_provider_cline_dry_run_masks_key(monkeypatch, tmp_path, capsys):
    # cline passes the token as a `--key` argv flag — the first harness to put a secret in
    # the command, so the dry-run command line must mask it.
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "cl.json").write_text(
        json.dumps(
            {
                "harness": "cline",
                "slug": "cline-anthropic",
                "secretEnv": "ANTHROPIC_API_KEY",
                "config": {"model": "claude-opus-4-8", "provider": "anthropic"},
            }
        )
    )
    monkeypatch.setenv("AGENTS_PROVIDERS_DIR", str(providers))
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-cline-supersecret\n")
    monkeypatch.setenv("AGENTS_ENV_FILE", str(env_file))
    _hermetic_sources(monkeypatch)
    monkeypatch.setitem(cli._COMPILERS, "cline", lambda project, global_, dest: cli.Plan())
    _no_launch(monkeypatch)
    monkeypatch.setattr("sys.argv", ["agedum", "--dry-run", "cl"])
    with pytest.raises(SystemExit) as exc:
        cli.app()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "cline" in out and "--key" in out  # the command line is shown
    assert "sk-cline-supersecret" not in out  # token masked in the argv
    assert "***" in out
