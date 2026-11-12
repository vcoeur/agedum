"""Authentication-aware OpenAI proxy routing, using fake credentials and localhost."""

import copy
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from test_failover_harness import _SSEFallbackUpstream, _WallUpstream
from test_proxy_trust import Upstream, request, route
from test_recovery_continuation import isolated_environment

from agedum.cli.main import _maybe_failover_proxy
from agedum.provider import FailoverPlan, build_launch, with_prompt
from agedum.proxy import CAPABILITY_HEADER, OPENAI_UPSTREAM_HEADER, FailoverProxy

PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1] / "agedum/assets/opencode/openai-routing"


@pytest.mark.parametrize("wait_enabled", [False, True])
@pytest.mark.parametrize("key,shape", [("plugins", "object"), ("plugin", "tuple")])
def test_launch_injects_routing_without_mutating_user_plugins(wait_enabled, key, shape):
    entry = {
        "object": {"package": str(PLUGIN_DIRECTORY), "options": {"baseURL": "stale"}},
        "tuple": [str(PLUGIN_DIRECTORY), {"baseURL": "stale"}],
    }[shape]
    config = {
        "harness": "opencode",
        "config": {
            "emitTranscript": False,
            "opencodeConfig": {key: ["user-plugin", entry]},
        },
    }
    original = copy.deepcopy(config)
    plan = FailoverPlan("http://127.0.0.1:1234", ("openai",), wait_enabled=wait_enabled)
    document = json.loads(build_launch(config, {}, failover=plan).env["OPENCODE_CONFIG_CONTENT"])
    assert document[key][0] == "user-plugin"
    assert document["plugins"][-1] == {
        "package": str(PLUGIN_DIRECTORY),
        "options": {"baseURL": plan.base_url},
    }
    assert json.dumps(document).count(str(PLUGIN_DIRECTORY)) == 1
    assert config == original


def test_no_failover_does_not_inject_openai_routing():
    config = {"harness": "opencode", "config": {"model": "openai/gpt-6.1-sol"}}
    document = json.loads(build_launch(config, {}).env["OPENCODE_CONFIG_CONTENT"])
    assert str(PLUGIN_DIRECTORY) not in json.dumps(document)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node exercises the JS plugin")
def test_plugin_tracks_active_authentication_and_scopes_the_selector():
    script = r"""
      import assert from 'node:assert/strict';
      const plugin = (await import(process.argv[1])).default;
      let credential, connection, reads = 0;
      const hooks = new Map();
      await plugin.setup({options:{baseURL:'http://127.0.0.1:1234'},
        session:{hook:async(name,callback)=>hooks.set(name,callback)},
        integration:{connection:{active:async id=>{
          assert.equal(id,'openai'); reads++; return connection;
        },resolve:async selected=>{assert.equal(selected,connection); return credential;}}}});
      assert.deepEqual([...hooks.keys()],['http.request']);
      const event = (url='http://127.0.0.1:1234/oc/openai/responses',providerID='openai') => ({
        model:{providerID,id:'gpt-6.1-sol'},request:new Request(url,{headers:{
          'authorization':'Bearer FAKE-ACCESS','X-Agedum-Proxy-Capability':'FAKE-CAPABILITY',
          'X-Agedum-OpenAI-Upstream':'stale'}})});
      const cases = [
        {type:'oauth',methodID:'chatgpt-token-sharing',expected:'api'},
        {type:'oauth',methodID:'chatgpt-browser',expected:'codex'},
        {type:'oauth',methodID:'chatgpt-headless',expected:'codex'},
        {type:'oauth',expected:'codex'},
        {type:'key',key:'FAKE-KEY',expected:'api'},
      ];
      for (const value of cases) {
        connection={type:'credential',id:'fake'}; credential=value;
        for (const kind of ['primary','title','generate','compaction']) {
          const current={...event(),kind};
          await hooks.get('http.request')(current);
          assert.equal(current.request.headers.get('X-Agedum-OpenAI-Upstream'),value.expected);
          assert.equal(current.request.headers.get('authorization'),'Bearer FAKE-ACCESS');
          assert.equal(current.request.headers.get('X-Agedum-Proxy-Capability'),'FAKE-CAPABILITY');
        }
      }
      connection={type:'env',name:'OPENAI_API_KEY'}; credential={type:'key',key:'FAKE-ENV'};
      const env=event(); await hooks.get('http.request')(env);
      assert.equal(env.request.headers.get('X-Agedum-OpenAI-Upstream'),'api');
      connection=undefined;
      const explicit=event(); await hooks.get('http.request')(explicit);
      assert.equal(explicit.request.headers.get('X-Agedum-OpenAI-Upstream'),'api');
      const before=reads;
      for (const current of [event(undefined,'other'),
        event('https://api.openai.com/v1/responses'),
        event('http://127.0.0.1:1235/oc/openai/responses'),
        event('http://localhost:1234/oc/openai/responses'),
        event('http://127.0.0.1:1234/oc/other/responses')]) {
        await hooks.get('http.request')(current);
        assert.equal(current.request.headers.get('X-Agedum-OpenAI-Upstream'),'stale');
      }
      assert.equal(reads,before);
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script, (PLUGIN_DIRECTORY / "index.js").as_uri()],
        check=True,
    )


def openai_spec(upstream):
    return {
        "routes": {"openai": route(upstream, "", openai=True)},
        "status": [429],
        "messages": ["quota"],
        "max_walk": 3,
        "chains": {},
        "vision": {},
    }


@pytest.mark.parametrize("selector", [None, "codex", "api"])
@pytest.mark.parametrize("method", ["POST", "GET", "HEAD", "OPTIONS"])
def test_proxy_selects_fixed_destination_and_strips_selector(monkeypatch, selector, method):
    with Upstream() as legacy, Upstream() as api:
        monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", api.url + "/v1")
        with FailoverProxy(openai_spec(legacy.url + "/codex")) as proxy:
            headers = [
                (CAPABILITY_HEADER, proxy.capability),
                ("Authorization", "Bearer FAKE-ACCESS"),
                ("ChatGPT-Account-Id", "FAKE-ACCOUNT"),
            ]
            if selector is not None:
                headers.append((OPENAI_UPSTREAM_HEADER, selector))
            status, _, _ = request(
                proxy.base_url + "/oc/openai/responses?x=1&x=%2F",
                {"model": "m", "input": []},
                headers,
                method,
            )
        assert status == 200
        selected, unused = (api, legacy) if selector == "api" else (legacy, api)
        assert unused.requests == []
        path, forwarded, _ = selected.requests[0]
        assert path == ("/v1" if selector == "api" else "/codex") + "/responses?x=1&x=%2F"
        forwarded = {name.lower(): value for name, value in forwarded}
        assert forwarded["authorization"] == "Bearer FAKE-ACCESS"
        assert forwarded["chatgpt-account-id"] == "FAKE-ACCOUNT"
        assert OPENAI_UPSTREAM_HEADER.lower() not in forwarded
        assert CAPABILITY_HEADER.lower() not in forwarded


@pytest.mark.parametrize("selectors", [["wrong"], ["https://example.invalid"], ["api", "codex"]])
def test_invalid_selector_fails_before_upstream_contact(selectors):
    with Upstream() as upstream, FailoverProxy(openai_spec(upstream.url)) as proxy:
        status, _, _ = request(
            proxy.base_url + "/oc/openai/responses",
            {"model": "m", "input": []},
            [(CAPABILITY_HEADER, proxy.capability)]
            + [(OPENAI_UPSTREAM_HEADER, value) for value in selectors],
        )
        assert status == 400
        assert upstream.requests == []


def test_selector_is_stripped_from_a_translated_fallback(monkeypatch):
    with _WallUpstream(429, {"error": {"message": "quota"}}) as api:
        with _SSEFallbackUpstream() as fallback:
            monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", api.base_url + "/v1")
            spec = openai_spec("http://127.0.0.1:1")
            spec["routes"]["fake"] = route(fallback.base_url + "/v1", "FAKE-FALLBACK")
            spec["chains"] = {"openai/m": ("fake/m",)}
            spec["vision"] = {"openai/m": True, "fake/m": True}
            with FailoverProxy(spec) as proxy:
                status, _, body = request(
                    proxy.base_url + "/oc/openai/responses",
                    {"model": "m", "input": []},
                    [
                        (CAPABILITY_HEADER, proxy.capability),
                        (OPENAI_UPSTREAM_HEADER, "api"),
                        ("Authorization", "Bearer FAKE-PRIMARY"),
                        ("ChatGPT-Account-Id", "FAKE-ACCOUNT"),
                    ],
                )
            assert status == 200
            assert b"FALLBACK-STUB-OK" in body
            assert api.requests[0][0] == "/v1/responses"
            path, headers, _ = fallback.requests[0]
            assert path == "/v1/chat/completions"
            headers = {name.lower(): value for name, value in headers.items()}
            assert headers["authorization"] == "Bearer FAKE-FALLBACK"
            assert "chatgpt-account-id" not in headers
            assert OPENAI_UPSTREAM_HEADER.lower() not in headers
            assert CAPABILITY_HEADER.lower() not in headers


def test_selector_cannot_redirect_another_provider(monkeypatch):
    with Upstream() as upstream:
        monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", "http://127.0.0.1:1")
        spec = openai_spec("http://127.0.0.1:1")
        spec["routes"]["fake"] = route(upstream.url, "FAKE-KEY")
        with FailoverProxy(spec) as proxy:
            status, _, _ = request(
                proxy.base_url + "/oc/fake/chat/completions",
                {"model": "m", "messages": []},
                [(CAPABILITY_HEADER, proxy.capability), (OPENAI_UPSTREAM_HEADER, "api")],
            )
        assert status == 200
        _, headers, _ = upstream.requests[0]
        assert OPENAI_UPSTREAM_HEADER.lower() not in dict(
            (name.lower(), value) for name, value in headers
        )


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for installed V2 localhost integration",
)
@pytest.mark.parametrize("method", ["chatgpt-token-sharing", "chatgpt-browser", "key"])
@pytest.mark.parametrize("wait_enabled", [False, True])
def test_installed_opencode_routes_active_credential(tmp_path, monkeypatch, method, wait_enabled):
    credential = {"type": "key", "key": "FAKE-KEY"}
    if method != "key":
        credential = {
            "type": "oauth",
            "methodID": method,
            "access": "FAKE-ACCESS",
            "refresh": "FAKE-REFRESH",
            "expires": 4102444800000,
            "metadata": {"accountId": "FAKE-ACCOUNT"},
        }
    environment = isolated_environment(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        service_port = listener.getsockname()[1]
    subprocess.run(
        ["opencode", "service", "set", "port", str(service_port)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=30,
        check=True,
    )
    try:
        created = subprocess.run(
            [
                "opencode",
                "api",
                "post",
                "/api/credential",
                "--data",
                json.dumps({"integrationID": "openai", "value": credential}),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert created.returncode == 0, created.stdout + created.stderr
    finally:
        subprocess.run(
            ["opencode", "service", "stop"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=30,
            check=True,
        )
    config = {
        "harness": "opencode",
        "config": {
            "model": "openai/gpt-6.1-sol",
            "standalone": True,
            "emitTranscript": False,
            "opencodeConfig": {
                "snapshots": False,
                "provider": {"openai": {"options": {"transport": "http"}}},
            },
        },
        "failover": {
            "detect": {"status": [429], "messages": ["quota"]},
            "wait": {"maxWaitHours": 1},
            "vision": {},
        },
    }
    if not wait_enabled:
        config["failover"] = {
            "detect": {"status": [429], "messages": ["quota"]},
            "chains": {"openai/gpt-6.1-sol": ["fake/m"]},
            "vision": {"openai/gpt-6.1-sol": True, "fake/m": True},
        }
        config["config"]["providerDef"] = {
            "id": "fake",
            "npm": "@ai-sdk/openai-compatible",
            "baseUrl": "http://127.0.0.1:1",
            "apiKeyEnv": "FAKE_KEY",
        }
        config["config"]["opencodeConfig"]["provider"]["fake"] = {"models": {"m": {}}}
    with Upstream(status=401, body=b'{"error":{"message":"LOCAL-API-ROUTE"}}') as api:
        with Upstream(status=401, body=b'{"error":{"message":"LOCAL-CODEX-ROUTE"}}') as legacy:
            monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", api.url + "/v1")
            monkeypatch.setattr("agedum.provider.OPENAI_CODEX_UPSTREAM", legacy.url + "/codex")
            with _maybe_failover_proxy(config, {"FAKE_KEY": "FAKE"}) as plan:
                launch = build_launch(config, {"FAKE_KEY": "FAKE"}, failover=plan)
                environment = {**isolated_environment(tmp_path), **launch.env}
                environment[plan.capability_env] = os.environ[plan.capability_env]
                result = subprocess.run(
                    with_prompt(launch, ["--format", "json"], "hello", interactive=False),
                    cwd=tmp_path,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
            selected, unused = (legacy, api) if method == "chatgpt-browser" else (api, legacy)
            assert result.returncode == 1, result.stdout + result.stderr
            assert selected.requests, result.stdout + result.stderr
            assert unused.requests == []
            expected = "LOCAL-CODEX-ROUTE" if selected is legacy else "LOCAL-API-ROUTE"
            assert expected in result.stdout
            for _, headers, _ in selected.requests:
                forwarded = {name.lower(): value for name, value in headers}
                assert forwarded["authorization"] == (
                    "Bearer FAKE-KEY" if method == "key" else "Bearer FAKE-ACCESS"
                )
                assert OPENAI_UPSTREAM_HEADER.lower() not in forwarded
                assert CAPABILITY_HEADER.lower() not in forwarded
