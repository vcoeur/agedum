"""Provider correctness regressions use synthetic configs, fake keys and localhost only."""

import json
import os
import shutil
import subprocess
import time
import tomllib
import urllib.request
from copy import deepcopy

import pytest
from test_carrier_expansion import SYNTH_CATALOGUE
from test_failover import _StubUpstream

from agedum.cli.main import _maybe_failover_proxy, _maybe_proxy, _run_config
from agedum.provider import (
    PROVIDER_SCHEMA_VERSION_2,
    ExpansionError,
    ProviderError,
    _render_codex_agent,
    _toml_escape,
    build_launch,
    expand_carrier_refs,
    failover_spec,
    parse_env_file,
)
from agedum.proxy import CAPABILITY_HEADER

SWITCHES = (
    "AGEDUM_FOLD_SYSTEM_MESSAGES",
    "AGEDUM_TRANSLATE_OPENAI",
    "AGEDUM_OPENAI_PROMPT_CACHE_KEY",
    "AGEDUM_OPENAI_THINKING",
    "AGEDUM_CODEX_CHAT_UPSTREAM",
)


def _apply_launch(monkeypatch, launch):
    for name, value in launch.env.items():
        monkeypatch.setenv(name, value)
    for name in launch.unset:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "kimi"])
def test_nested_launch_clears_only_internal_switches(monkeypatch, harness):
    for name in SWITCHES:
        monkeypatch.setenv(name, "stale")
    monkeypatch.setenv("AGEDUM_USER_VALUE", "retain")
    parent = build_launch(
        {
            "harness": "claude",
            "secretEnv": "FAKE",
            "config": {"baseUrl": "http://127.0.0.1:9", "upstreamApi": "openai-completions"},
        },
        {"FAKE": "fake"},
    )
    _apply_launch(monkeypatch, parent)
    child = build_launch({"harness": harness, "config": {}}, dict(os.environ))
    _apply_launch(monkeypatch, child)
    assert not any(name in os.environ for name in SWITCHES)
    assert os.environ["AGEDUM_USER_VALUE"] == "retain"
    assert os.environ["FAKE"] == "fake"


def test_nested_launch_current_proxy_protocol_reaches_local_wire(monkeypatch):
    with _StubUpstream() as upstream:
        for name in SWITCHES:
            monkeypatch.setenv(name, "1")
        config = {
            "harness": "claude",
            "secretEnv": "FAKE",
            "config": {"baseUrl": upstream.base_url, "foldSystemMessages": True},
        }
        launch = build_launch(config, {"FAKE": "fake-key"})
        _apply_launch(monkeypatch, launch)
        with _maybe_proxy("claude"):
            request = urllib.request.Request(
                os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
                data=json.dumps({"model": "m", "messages": []}).encode(),
                headers={
                    "Content-Type": "application/json",
                    CAPABILITY_HEADER: os.environ["ANTHROPIC_CUSTOM_HEADERS"].split(": ", 1)[1],
                },
            )
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
        assert upstream.requests[0][1] == "/v1/messages"
        assert "AGEDUM_TRANSLATE_OPENAI" not in os.environ


def _expand_default(tmp_path, key, extra=None):
    (tmp_path / "models.yaml").write_text(SYNTH_CATALOGUE)
    config = {"harness": "opencode", "config": {"model": f"{key}@low", **(extra or {})}}
    return expand_carrier_refs(config, root_schema=PROVIDER_SCHEMA_VERSION_2, base_dir=tmp_path)


@pytest.mark.parametrize(
    "key,provider,model",
    [
        ("ds-flash", "ds", "ds-flash"),
        ("glm-x", "glm-p", "glm-x"),
        ("sol", "openai", "sol"),
        ("k3", "kimi-coding", "k3-low"),
    ],
)
def test_default_carrier_effort_survives_builder_and_explicit_agents(
    tmp_path, key, provider, model
):
    agent = {
        "model": f"{provider}/{model}",
        "variant": "high",
        "options": {"reasoningEffort": "high"},
    }
    expanded = _expand_default(tmp_path, key, {"opencodeConfig": {"agent": {"explicit": agent}}})
    document = json.loads(build_launch(expanded, {}).env["OPENCODE_CONFIG_CONTENT"])
    assert document["model"] == f"{provider}/{model}"
    assert document["agent"]["explicit"] == agent
    options = document["provider"][provider]["models"][model]["options"]
    if key == "k3":
        assert options["thinking"] == {"type": "enabled", "effort": "low"}
    else:
        assert options["reasoningEffort"] == "low"
    if key == "sol":
        assert "high" not in document["provider"][provider]["models"][model]["variants"]


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE_WIRE") != "1",
    reason="opt-in installed OpenCode localhost wire probe",
)
@pytest.mark.parametrize(
    "key,provider,model",
    [
        ("ds-flash", "ds", "ds-flash"),
        ("glm-x", "glm-p", "glm-x"),
        ("sol", "openai", "sol"),
        ("k3", "kimi-coding", "k3-low"),
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_installed_opencode_default_effort_local_wire(
    tmp_path, key, provider, model, explicit, composition=None
):
    binary = shutil.which("opencode")
    assert binary
    with _StubUpstream(
        script=[
            (400, '{"error":{"message":"local probe stop"}}', {"Content-Type": "application/json"})
        ]
    ) as upstream:
        extra = {}
        if explicit:
            agent = {"model": f"{provider}/{model}", "mode": "primary"}
            if key == "sol":
                agent["variant"] = "high"
            elif key == "k3":
                agent["model"] = "k3@high"
            else:
                agent["options"] = {"reasoningEffort": "high"}
            extra = {"opencodeConfig": {"agent": {"wire": agent}}}
        if composition is not None:
            extra = _gpt_agent_composition(composition)
        expanded = _expand_default(tmp_path, key, extra)
        block = expanded["config"]
        npm = "@ai-sdk/openai" if key == "sol" else "@ai-sdk/openai-compatible"
        block["providerDef"] = {
            "id": provider,
            "npm": npm,
            "baseUrl": upstream.base_url,
            "apiKeyEnv": "FAKE",
        }
        entry = block["opencodeConfig"]["provider"][provider]["models"][model]
        entry.update({"reasoning": True, "limit": {"context": 32768, "output": 1024}})
        document = json.loads(
            build_launch(expanded, {"FAKE": "fake-key"}).env["OPENCODE_CONFIG_CONTENT"]
        )
        selected_model = document.get("agent", {}).get("wire", {}).get("model", "")
        wire_model = selected_model.partition("/")[2] if explicit else model
        document.update(
            {
                "autoupdate": False,
                "plugin": [],
                "share": "disabled",
                "enabled_providers": [provider],
            }
        )
        document.setdefault("agent", {}).setdefault("wire" if explicit else "build", {})[
            "prompt"
        ] = "WIRE-MAIN"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "OPENCODE_CONFIG_CONTENT": json.dumps(document),
            "OPENCODE_DISABLE_MODELS_FETCH": "true",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        command = [binary, "run", "--pure", "--format", "json"]
        if explicit:
            command.extend(["--agent", "wire"])
        process = subprocess.Popen(
            [*command, "hello"],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def selected_requests():
            return [
                json.loads(request[3])
                for request in upstream.requests
                if json.loads(request[3]).get("model") == ("k3" if key == "k3" else wire_model)
                and "WIRE-MAIN" in request[3].decode()
            ]

        try:
            deadline = time.monotonic() + 20
            while (
                not selected_requests() and process.poll() is None and time.monotonic() < deadline
            ):
                time.sleep(0.01)
        finally:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=10)
        assert selected_requests(), stdout + stderr
        body = selected_requests()[0]
        expected = "high" if explicit else "low"
        if key == "k3":
            assert body["thinking"]["effort"] == expected
        elif key == "sol":
            assert body["reasoning"]["effort"] == expected, body
        else:
            assert body["reasoning_effort"] == expected


def _gpt_agent_composition(composition):
    native = {"mode": "primary", "variant": "high"}
    row = {"agent": "wire", "model": "openai/sol"}
    if composition == "modeled-model-native-variant":
        pass
    elif composition == "modeled-effort-native-model":
        row = {"agent": "wire", "reasoningEffort": "high"}
        native = {"mode": "primary", "model": "openai/sol"}
    elif composition == "native-model-wins-sol":
        row["model"] = "openai/other"
        native["model"] = "openai/sol"
    elif composition == "native-model-wins-other":
        native["model"] = "openai/other"
    elif composition == "last-modeled-row-wins-other":
        return {
            "agentOptions": [row, {"agent": "wire", "model": "openai/other"}],
            "opencodeConfig": {
                "agent": {"wire": native},
                "provider": {"openai": {"models": {"other": _other_gpt_model()}}},
            },
        }
    else:
        raise AssertionError(composition)
    return {
        "agentOptions": [row],
        "opencodeConfig": {
            "agent": {"wire": native},
            "provider": {"openai": {"models": {"other": _other_gpt_model()}}},
        },
    }


def _other_gpt_model():
    return {
        "name": "Other",
        "reasoning": True,
        "limit": {"context": 32768, "output": 1024},
        "variants": {"high": {"reasoningEffort": "high"}},
    }


COMPOSITIONS = [
    "modeled-model-native-variant",
    "modeled-effort-native-model",
    "native-model-wins-sol",
    "native-model-wins-other",
    "last-modeled-row-wins-other",
]


@pytest.mark.parametrize("composition", COMPOSITIONS)
def test_gpt_variant_preservation_uses_runtime_agent_composition(tmp_path, composition):
    extra = _gpt_agent_composition(composition)
    before = deepcopy(extra)
    expanded = _expand_default(tmp_path, "sol", extra)
    document = json.loads(build_launch(expanded, {}).env["OPENCODE_CONFIG_CONTENT"])
    agent = document["agent"]["wire"]
    model = "other" if composition.endswith("other") else "sol"
    assert agent["model"] == f"openai/{model}"
    variants = document["provider"]["openai"]["models"]["sol"]["variants"]
    if model == "sol" and agent.get("variant") == "high":
        assert "high" not in variants
    else:
        assert variants["high"] == {"disabled": True}
    assert document["provider"]["openai"]["models"]["sol"]["options"]["reasoningEffort"] == "low"
    assert extra == before


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE_WIRE") != "1",
    reason="opt-in installed OpenCode localhost composition wire probe",
)
@pytest.mark.parametrize("composition", COMPOSITIONS)
def test_installed_opencode_composed_gpt_variant_wire(tmp_path, composition):
    test_installed_opencode_default_effort_local_wire(
        tmp_path, "sol", "openai", "sol", True, composition
    )


@pytest.mark.parametrize(
    "extra", [{"effortLevel": "high"}, {"defaultOptions": {"reasoningEffort": "high"}}]
)
def test_default_carrier_conflicting_authored_effort_fails(tmp_path, extra):
    with pytest.raises(ExpansionError, match="default effort"):
        _expand_default(tmp_path, "sol", extra)


@pytest.mark.parametrize(
    "source",
    [
        'developer_instructions = """\nsandbox_mode = "read-only"\n"""\n',
        'name = "worker"\n[tools]\nenabled = true\n',
        'name = "worker"\n[tools]\nsandbox_mode = "read-only"\n',
    ],
)
def test_codex_default_is_structurally_rooted_and_preserves_source(source):
    rendered = _render_codex_agent(source)
    assert rendered.endswith(source)
    assert tomllib.loads(rendered)["sandbox_mode"] == "workspace-write"


def test_codex_explicit_root_and_invalid_source():
    source = '"sandbox_mode" = "read-only"\n[tools]\nenabled = true\n'
    assert _render_codex_agent(source) == source
    with pytest.raises(ProviderError, match="invalid codex agent TOML"):
        _render_codex_agent('name = "unterminated')


def test_codex_builder_copy_parses_root_default_and_del(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sources = tmp_path / "agents"
    sources.mkdir()
    source = 'developer_instructions = """\nsandbox_mode = "read-only"\n"""\n[tools]\nx = true\n'
    (sources / "worker.toml").write_text(source)
    launch = build_launch(
        {
            "harness": "codex",
            "config": {
                "codexAgents": str(sources),
                "codexConfig": {"developer_instructions": "fake\x7f"},
            },
        },
        {},
    )
    target, content = launch.config_files[0][:2]
    copied = tmp_path / "copied-worker.toml"
    copied.write_text(content)
    assert target.endswith("/agents/worker.toml")
    assert tomllib.loads(copied.read_text())["sandbox_mode"] == "workspace-write"
    override = next(
        value for value in launch.command if value.startswith("developer_instructions=")
    )
    assert tomllib.loads(override)["developer_instructions"] == "fake\x7f"


@pytest.mark.parametrize(
    "harness,env_name", [("kimi", "KIMI_CODE_HOME"), ("cline", "CLINE_DATA_DIR")]
)
def test_exact_pair_cache_identity_and_intentional_sharing(
    monkeypatch, tmp_path, harness, env_name
):
    monkeypatch.setenv("HOME", str(tmp_path))

    def launch(endpoint, model, token="fake-one"):
        return build_launch(
            {
                "harness": harness,
                "secretEnv": "FAKE",
                "config": {"baseUrl": endpoint, "model": model},
            },
            {"FAKE": token},
        )

    first = launch("http://127.0.0.1/a_b", "Model")
    paths = {
        first.env[env_name],
        launch("http://127.0.0.1/a-b", "Model").env[env_name],
        launch("http://127.0.0.1/a_b", "model").env[env_name],
    }
    assert len(paths) == 3
    assert launch("http://127.0.0.1/a_b", "Model", "fake-two").env[env_name] == first.env[env_name]
    assert "fake" not in first.env[env_name]


def _failover_config(endpoint="http://127.0.0.1:9"):
    return {
        "harness": "opencode",
        "config": {
            "model": "openai/gpt-test",
            "providerDef": {
                "id": "p",
                "npm": "@ai-sdk/openai-compatible",
                "baseUrl": endpoint,
                "apiKeyEnv": "FAKE",
            },
            "opencodeConfig": {"provider": {"p": {"models": {"m": {"name": "M"}}}}},
        },
        "failover": {
            "detect": {"status": [429], "messages": ["limit"]},
            "vision": {"openai/gpt-test": False, "p/m": False},
            "chains": {"openai/gpt-test": ["p/m"]},
        },
    }


@pytest.mark.parametrize("surface", ["model", "opencodeConfig", "agentOptions"])
def test_effective_openai_primary_seeds_failover(surface):
    config = _failover_config()
    block = config["config"]
    if surface != "model":
        block.pop("model")
    if surface == "opencodeConfig":
        block["opencodeConfig"]["model"] = "openai/gpt-test"
    if surface == "agentOptions":
        block["agentOptions"] = [{"agent": "worker", "model": "openai/gpt-test"}]
    spec, _ = failover_spec(config, {"FAKE": "fake-key"})
    assert spec["routes"]["openai"]["keys_by_wire"]["gpt-test"] == ["gpt-test"]


def test_default_only_openai_failover_reaches_local_fallback(monkeypatch):
    with _StubUpstream() as primary, _StubUpstream() as fallback:
        config = _failover_config(fallback.base_url)
        monkeypatch.setattr("agedum.provider.OPENAI_CODEX_UPSTREAM", primary.base_url)
        primary.script = [(429, "limit", {})]
        with _maybe_failover_proxy(config, {"FAKE": "fake-key"}) as plan:
            launch = build_launch(config, {"FAKE": "fake-key"}, failover=plan)
            document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
            url = document["provider"]["openai"]["options"]["baseURL"] + "/custom"
            request = urllib.request.Request(
                url,
                data=b'{"model":"gpt-test"}',
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer fake-oauth",
                    CAPABILITY_HEADER: os.environ["AGEDUM_PROXY_CAPABILITY"],
                },
            )
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
        assert json.loads(fallback.requests[0][3])["model"] == "m"
        assert fallback.requests[0][2]["Authorization"] == "Bearer fake-key"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"FAKE" # comment', "FAKE"),
        ("'FAKE # value' # comment", "FAKE # value"),
        ('"FAKE # value"\t# comment', "FAKE # value"),
        ("FAKE#value # comment", "FAKE#value"),
        ('"$(false) ${HOME}" # comment', "$(false) ${HOME}"),
        ('"fake\\"quote" # comment', 'fake\\"quote'),
        ("'' # empty", ""),
    ],
)
def test_dotenv_quote_comment_matrix(tmp_path, raw, expected):
    path = tmp_path / "fake.env"
    path.write_text(f"export FAKE={raw}\n")
    assert parse_env_file(path) == {"FAKE": expected}


@pytest.mark.parametrize("raw", ['"unclosed', '"value"garbage', '"value"#comment'])
def test_dotenv_invalid_quoted_suffix_fails_without_value_leak(tmp_path, raw):
    path = tmp_path / "fake.env"
    path.write_text(f"FAKE={raw}\n")
    with pytest.raises(ProviderError) as error:
        parse_env_file(path)
    assert raw not in str(error.value)


@pytest.mark.parametrize("code", [*range(32), 127])
def test_toml_control_roundtrip(code):
    value = 'fake"\\é' + chr(code)
    assert tomllib.loads(f'value = "{_toml_escape(value)}"')["value"] == value


@pytest.mark.parametrize("block", [{}, [], False, "", 0, "malformed"])
@pytest.mark.parametrize("harness", ["opencode", "claude"])
def test_present_malformed_failover_fails_spec_and_cli_dispatch(block, harness, tmp_path):
    config = {"harness": harness, "failover": block}
    with pytest.raises(ProviderError):
        failover_spec(config, {})
    with pytest.raises(ProviderError):
        with _maybe_failover_proxy(config, {}):
            pytest.fail("malformed declaration bypassed validation")
    source = tmp_path / "provider.json"
    source.write_text(json.dumps(config))
    dotenv = tmp_path / "fake.env"
    dotenv.write_text("FAKE=fake-key\n")
    assert _run_config([str(source), "--env", str(dotenv), "--dry-run"]) == 1


@pytest.mark.parametrize(
    "config", [{"harness": "claude"}, {"harness": "opencode", "failover": None}]
)
def test_absent_null_failover_remains_disabled(config):
    assert failover_spec(config, {}) == (None, [])
    with _maybe_failover_proxy(config, {}) as plan:
        assert plan is None


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_wait_rejected_before_proxy(number):
    config = _failover_config()
    config["failover"]["wait"] = {"maxWaitHours": number}
    with pytest.raises(ProviderError, match="finite"):
        failover_spec(config, {})
    with pytest.raises(ProviderError, match="finite"):
        with _maybe_failover_proxy(config, {}):
            pytest.fail("nonfinite wait reached launch")


def test_large_finite_wait_preserved_without_new_upper_limit():
    config = _failover_config()
    config["failover"]["wait"] = {"maxWaitHours": 1000000, "probeSeconds": 17}
    before = deepcopy(config)
    assert failover_spec(config, {})[0]["wait"] == {"maxWaitHours": 1000000, "probeSeconds": 17}
    assert config == before
