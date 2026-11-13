import copy
import io
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_proxy_trust import Upstream

from agedum.cli.main import _maybe_failover_proxy
from agedum.provider import FailoverPlan, build_launch, with_prompt
from agedum.proxy import _retry_after_seconds, responses_to_chat_request, translate_chat_stream

PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1] / "agedum/assets/opencode"
PLUGIN = PLUGIN_DIRECTORY / "recovery-continuation.js"
REMINDER = (
    "If this task resumes after a temporary provider-limit wait, continue the original task "
    "from its recorded state. Do not treat an earlier usage-limit error as a permanent block "
    "or as a review result. Reconcile unfinished worker work before retrying it; do not repeat "
    "completed side effects. This instruction does not authorize changing provider/model or "
    "bypassing permissions/review."
)
SUBSCRIPTION_CODE = "subscription_sharing_usage_limit_exceeded"
SUBSCRIPTION_MESSAGE = (
    "The ChatGPT user has reached their Subscription Sharing usage limit. "
    "Ask the user to try again after their usage limit resets or use an API key instead."
)


@pytest.mark.parametrize("emit_transcript", [True, False])
@pytest.mark.parametrize("chained", [True, False])
def test_wait_plan_injects_v2_plugin_without_mutating_config(emit_transcript, chained):
    config = launcher_config("http://127.0.0.1:1", emit_transcript=emit_transcript)
    if chained:
        config["failover"].update(
            chains={"fake/main": ["fake/worker"]},
            vision={"fake/main": False, "fake/worker": False},
        )
    config["config"]["opencodeConfig"].update(
        plugin=["legacy-plugin", ["legacy-options", {"strict": True}]],
        plugins=[{"package": "native-plugin", "options": {"strict": False}}],
    )
    original = copy.deepcopy(config)
    with _maybe_failover_proxy(config, {"FAKE_KEY": "fake"}) as plan:
        assert plan.wait_enabled is True
        launch = build_launch(config, {"FAKE_KEY": "fake"}, failover=plan)
    document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    assert document["plugins"] == [
        {"package": "native-plugin", "options": {"strict": False}},
        str(PLUGIN_DIRECTORY),
        {
            "package": str(PLUGIN_DIRECTORY / "openai-routing"),
            "options": {"baseURL": plan.base_url},
        },
    ]
    assert document["plugin"][:2] == original["config"]["opencodeConfig"]["plugin"]
    assert any("transcript-osc.js" in str(entry) for entry in document["plugin"]) is emit_transcript
    assert config == original


@pytest.mark.parametrize(
    "key,shape",
    [
        ("plugin", "string"),
        ("plugin", "object"),
        ("plugin", "tuple"),
        ("plugins", "string"),
        ("plugins", "object"),
    ],
)
def test_recovery_plugin_is_not_duplicated_in_user_entries(key, shape):
    entry = {
        "string": str(PLUGIN_DIRECTORY),
        "object": {"package": str(PLUGIN_DIRECTORY), "options": {}},
        "tuple": [str(PLUGIN_DIRECTORY), {}],
    }[shape]
    config = {
        "harness": "opencode",
        "config": {"emitTranscript": False, "opencodeConfig": {key: [entry]}},
    }
    plan = FailoverPlan("http://127.0.0.1:1", (), wait_enabled=True)
    document = json.loads(build_launch(config, {}, failover=plan).env["OPENCODE_CONFIG_CONTENT"])
    assert document[key] == [entry]
    if key == "plugin":
        assert document["plugins"] == []


def test_no_wait_plan_keeps_existing_plugin_documents_unchanged():
    config = {"harness": "opencode", "config": {"opencodeConfig": {"plugins": ["native-plugin"]}}}
    before = json.loads(build_launch(config, {}).env["OPENCODE_CONFIG_CONTENT"])
    plan = FailoverPlan("http://127.0.0.1:1", ())
    after = json.loads(build_launch(config, {}, failover=plan).env["OPENCODE_CONFIG_CONTENT"])
    assert after == {**before, "provider": {}}
    assert str(PLUGIN_DIRECTORY) not in after["plugins"]


def test_chained_failover_without_wait_does_not_enable_reminder():
    config = launcher_config("http://127.0.0.1:1")
    config["failover"].pop("wait")
    config["failover"].update(
        chains={"fake/main": ["fake/worker"]},
        vision={"fake/main": False, "fake/worker": False},
    )
    with _maybe_failover_proxy(config, {"FAKE_KEY": "fake"}) as plan:
        assert plan.wait_enabled is False
        document = json.loads(
            build_launch(config, {"FAKE_KEY": "fake"}, failover=plan).env["OPENCODE_CONFIG_CONTENT"]
        )
    assert document["plugins"] == [
        {
            "package": str(PLUGIN_DIRECTORY / "openai-routing"),
            "options": {"baseURL": plan.base_url},
        }
    ]


def test_retry_plugin_options_are_proxy_scoped_and_replace_only_owned_entries():
    config = launcher_config("http://127.0.0.1:1")
    config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
    config["config"]["opencodeConfig"].update(
        plugin=[str(PLUGIN_DIRECTORY), "user-legacy"],
        plugins=[{"package": str(PLUGIN_DIRECTORY), "options": {}}, "user-native"],
    )
    original = copy.deepcopy(config)
    with _maybe_failover_proxy(config, {"FAKE_KEY": "fake"}) as plan:
        document = json.loads(
            build_launch(config, {"FAKE_KEY": "fake"}, failover=plan).env["OPENCODE_CONFIG_CONTENT"]
        )
        assert document["plugin"] == ["user-legacy"]
        assert document["plugins"] == [
            "user-native",
            {
                "package": str(PLUGIN_DIRECTORY),
                "options": {
                    "baseURL": plan.base_url,
                    "routes": list(plan.routes),
                    "retryMessages": ["usage limit", "rate limit"],
                    "maxWaitHours": 8,
                },
            },
            {
                "package": str(PLUGIN_DIRECTORY / "openai-routing"),
                "options": {"baseURL": plan.base_url},
            },
        ]
        assert plan.capability_env not in json.dumps(document["plugins"])
    assert config == original


@pytest.mark.skipif(shutil.which("node") is None, reason="Node exercises the JS plugin")
def test_retry_hooks_scope_consume_expire_and_fail_closed_on_overlap():
    script = r"""
      import assert from 'node:assert/strict';
      const plugin = (await import(process.argv[1])).default;
      const hooks = new Map();
      await plugin.setup({options:{baseURL:'http://127.0.0.1:1234', routes:['fake'],
        maxWaitHours:1/3600, retryMessages:['usage limit', 'rate limit']},
        session:{hook:async(name, callback)=>hooks.set(name, callback)}});
      assert.deepEqual([...hooks.keys()], ['context','http.request','http.response','retry']);
      const base = {sessionID:'main',agent:'worker',model:{providerID:'fake',id:'m'},
        kind:'primary',request:new Request('http://127.0.0.1:1234/oc/fake/chat/completions')};
      const response = (overrides={}) => ({...base,
        response:new Response('usage limit',{status:429,headers:{'retry-after':'1',
          'x-agedum-retry-eligible':'1'}}), ...overrides});
      const retry = (overrides={}) => ({...base,error:{status:429,type:'provider.quota',
        message:'usage limit'},attempt:2,decision:{retry:false},...overrides});
      async function observed(overrides={}) {
        const event=response(overrides);
        await hooks.get('http.request')(event); await hooks.get('http.response')(event);
      }
      async function unchanged(event) {
        const before=structuredClone(event.decision);
        await hooks.get('retry')(event); assert.deepEqual(event.decision,before);
      }
      await observed();
      for (const overrides of [{sessionID:'other'}, {agent:'other'},
        {model:{providerID:'fake',id:'other'}}, {model:{providerID:'other',id:'m'}},
        {model:{providerID:'fake',id:'m',variant:'high'}}]) await unchanged(retry(overrides));
      const accepted=retry(); await hooks.get('retry')(accepted);
      assert.deepEqual(accepted.decision,{retry:true,delay:1000});
      await unchanged(retry());
      // Native generic rate-limit retries must also use the accepted one-second hint.
      await observed(); const generic=retry({error:{status:429,type:'provider.rate-limit',
        message:'rate limit'},decision:{retry:true,delay:9000}});
      await hooks.get('retry')(generic); assert.equal(generic.decision.delay,1000);
      const realNow=Date.now; Date.now=()=>Date.UTC(2026,10,5,0,0,0);
      await observed({response:new Response('usage limit',{status:429,headers:{
        'x-agedum-retry-eligible':'1','retry-after':'Thu, 05 Nov 2026 00:00:01 GMT'}})});
      const dated=retry(); await hooks.get('retry')(dated);
      assert.deepEqual(dated.decision,{retry:true,delay:1000}); Date.now=realNow;
      for (const kind of ['title','generate','compaction']) {
        await observed({kind}); await unchanged(retry());
      }
      for (const headers of [{}, {'x-agedum-retry-eligible':'spoof','retry-after':'1'},
        {'x-agedum-retry-eligible':'1','retry-after':'2'},
        {'x-agedum-retry-eligible':'1','retry-after':'NaN'},
        {'x-agedum-retry-eligible':'1','retry-after':'-1'},
        {'x-agedum-retry-eligible':'1','retry-after':'1, 1'}]) {
        await observed({response:new Response('usage limit',{status:429,headers})});
        await unchanged(retry());
      }
      await observed({response:new Response('usage limit',{status:402,headers:{
        'retry-after':'1','x-agedum-retry-eligible':'1'}})}); await unchanged(retry());
      for (const url of ['http://localhost:1234/oc/fake/chat/completions',
        'http://127.0.0.1:1235/oc/fake/chat/completions',
        'http://127.0.0.1:1234/oc/other/chat/completions']) {
        await observed({request:new Request(url)}); await unchanged(retry());
      }
      for (const message of ['usage limit: balance', 'rate limit: auth',
        'usage limit: billing', 'usage limit: configuration', 'quota']) {
        await observed(); await unchanged(retry({error:{status:429,message}}));
        await unchanged(retry());
      }
      await observed(); await unchanged(retry({error:{status:402,message:'usage limit'}}));
      await unchanged(retry());
      await observed(); await unchanged(retry({error:{status:429,message:'usage limit',
        type:'provider.authentication'}})); await unchanged(retry());
      await observed(); const now=Date.now; Date.now=()=>now()+31_000;
      await unchanged(retry()); Date.now=now;
      // A new response without a tracked request cannot leave or manufacture a marker.
      await observed(); await hooks.get('http.response')(response()); await unchanged(retry());
      const overlap={...base,sessionID:'overlap'};
      await hooks.get('http.request')(overlap); await hooks.get('http.request')(overlap);
      await hooks.get('http.response')(response(overlap));
      await hooks.get('http.response')(response(overlap));
      await unchanged(retry(overlap));
      await observed(overlap); await unchanged(retry(overlap));
      // Overlap with an auxiliary flow is equally ambiguous because retry omits kind.
      const mixed={...base,sessionID:'mixed'};
      await hooks.get('http.request')(mixed);
      await hooks.get('http.request')({...mixed,kind:'generate'});
      await hooks.get('http.response')(response(mixed)); await unchanged(retry(mixed));
      // A response followed by another request before consumption is also unsafe.
      const stale={...base,sessionID:'stale'};
      await observed(stale); await hooks.get('http.request')(stale);
      await hooks.get('http.response')(response(stale)); await unchanged(retry(stale));
      // A transport failure without response never picks up an older marker.
      const transport={...base,sessionID:'transport'};
      await hooks.get('http.request')(transport); await unchanged(retry(transport));
      await observed(transport); await unchanged(retry(transport));
      // Fail-closed keys do not poison independent sessions.
      await observed({sessionID:'independent'}); const independent=retry({sessionID:'independent'});
      await hooks.get('retry')(independent); assert.equal(independent.decision.retry,true);
    """
    subprocess.run(["node", "--input-type=module", "-e", script, PLUGIN.as_uri()], check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node exercises the JS plugin")
@pytest.mark.parametrize(
    "style,timezone,year",
    [
        ("imf", "UTC", 2026),
        ("rfc850", "UTC", 2026),
        ("asctime", "UTC", 2026),
        ("asctime", "Etc/GMT+7", 2026),
        ("rfc850", "UTC", 2068),
        ("rfc850", "UTC", 1969),
    ],
)
def test_proxy_accepted_http_date_reaches_retry_hook_in_utc(monkeypatch, style, timezone, year):
    now = datetime(year, 11, 5, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("agedum.proxy.datetime", FrozenDatetime)
    hint = {
        "imf": now.strftime("%a, %d %b %Y 00:00:01 GMT"),
        "rfc850": now.strftime("%A, %d-%b-%y 00:00:01 GMT"),
        "asctime": now.strftime("%a %b ") + f"{now.day:2d}" + now.strftime(" 00:00:01 %Y"),
    }[style]
    with RecoveryUpstream(429, wall_message="usage limit", retry_after=hint) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        config["failover"]["wait"].update(maxWaitHours=2 / 3600, retryMessages=["usage limit"])
        with _maybe_failover_proxy(config, {"FAKE_KEY": "fake"}) as plan:
            request_url = f"{plan.base_url}/oc/fake/chat/completions"
            request = urllib.request.Request(
                request_url,
                data=json.dumps(
                    {
                        "model": "main",
                        "messages": [{"role": "system", "content": "MAIN-ONLY"}],
                    }
                ).encode(),
                headers={"X-Agedum-Proxy-Capability": os.environ[plan.capability_env]},
            )
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            response = caught.value
            assert response.code == 429
            assert response.read() == b'{"error": {"message": "usage limit"}}'
            assert response.headers["Retry-After"] == hint
            assert response.headers["X-Agedum-Retry-Eligible"] == "1"
            assert _retry_after_seconds(list(response.headers.items())) == 1
            fixture = {
                "options": {
                    "baseURL": plan.base_url,
                    "routes": ["fake"],
                    "retryMessages": ["usage limit"],
                    "maxWaitHours": 2 / 3600,
                },
                "url": request_url,
                "headers": dict(response.headers),
                "now": now.timestamp() * 1000,
                "timezone": timezone,
            }
    script = r"""
      import assert from 'node:assert/strict';
      const fixture=JSON.parse(process.argv[2]);
      Date.now=()=>fixture.now;
      if(fixture.timezone==='Etc/GMT+7')
        assert.equal(new Date(fixture.now).getTimezoneOffset(),420);
      const hooks=new Map();
      const plugin=(await import(process.argv[1])).default;
      await plugin.setup({options:fixture.options,
        session:{hook:async(name,callback)=>hooks.set(name,callback)}});
      const base={sessionID:'date-session',agent:'main',model:{providerID:'fake',id:'main'},
        kind:'primary',request:new Request(fixture.url)};
      const retry=()=>({...base,error:{status:429,type:'provider.quota',message:'usage limit'},
        attempt:2,decision:{retry:false}});
      const observe=async headers=>{
        await hooks.get('http.request')(base);
        await hooks.get('http.response')({...base,response:new Response('usage limit',
          {status:429,headers})});
      };
      await observe(fixture.headers);
      const accepted=retry(); await hooks.get('retry')(accepted);
      assert.deepEqual(accepted.decision,{retry:true,delay:1000});
      const consumed=retry(); await hooks.get('retry')(consumed);
      assert.deepEqual(consumed.decision,{retry:false});
      const immediateHeaders=new Headers(fixture.headers);
      immediateHeaders.set('retry-after',immediateHeaders.get('retry-after').replace(':01',':00'));
      await observe(immediateHeaders); const immediate=retry(); await hooks.get('retry')(immediate);
      assert.deepEqual(immediate.decision,{retry:true,delay:0});
      await observe(fixture.headers); Date.now=()=>fixture.now+31_000;
      const stale=retry(); await hooks.get('retry')(stale);
      assert.deepEqual(stale.decision,{retry:false}); Date.now=()=>fixture.now;
      for(const hint of ['Thu, 31 Feb 2026 00:00:01 GMT',
        'Thursday, 31-Feb-26 00:00:01 GMT','Thu Feb 31 00:00:01 2026',
        'Thu Nov  5 00:00:01 2026 GMT', 'November 5, 2026 00:00:01',
        'Thursday, 05-Nov-26 00:00:99 GMT','Thu Nov  5 25:00:01 2026',
        'Fri, 05 Nov 9999 00:00:01 GMT', 'Friday, 05-Nov-99 00:00:01 GMT']) {
        if(hint.startsWith('Friday,') && fixture.now>Date.UTC(2000,0,1)) continue;
        const headers=new Headers(fixture.headers); headers.set('retry-after',hint);
        await observe(headers); const ignored=retry(); await hooks.get('retry')(ignored);
        assert.deepEqual(ignored.decision,{retry:false},hint);
      }
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script, PLUGIN.as_uri(), json.dumps(fixture)],
        env={"PATH": os.environ["PATH"], "TZ": timezone},
        check=True,
    )


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is required to exercise the JS plugin"
)
def test_v2_hook_changes_only_one_owned_system_part_per_context():
    script = """
      import assert from 'node:assert/strict';
      const plugin = (await import(process.argv[1])).default;
      assert.equal(plugin.id, 'agedum.recovery-continuation');
      const hooks = new Map();
      await plugin.setup({session: {hook: async (name, hook) => {
        assert.equal(name, 'context'); assert.equal(hooks.size, 0); hooks.set(name, hook);
      }}});
      const results = [];
      for (const agent of ['main', 'worker']) {
        for (let call = 0; call < 2; call++) {
          const existing = {type:'text', text:'original'};
          const event = {sessionID: agent, agent, model: {providerID:'fake', id:'main'},
            system:[existing], messages:[{role:'user', content:'original prompt'}],
            tools:{write:{description:'original tool'}}, options:{temperature:0.2}};
          const original = structuredClone(event);
          const messages = event.messages, tools = event.tools, options = event.options;
          await hooks.get('context')(event);
          assert.equal(event.system[0], existing);
          assert.equal(event.system.length, 2);
          assert.equal(event.messages, messages); assert.equal(event.tools, tools);
          assert.equal(event.options, options);
          assert.deepEqual({...event, system:original.system}, original);
          results.push(event.system[1]);
        }
      }
      assert.equal(hooks.size, 1);
      console.log(JSON.stringify(results));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, PLUGIN.as_uri()],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [{"type": "text", "text": REMINDER}] * 4


def launcher_config(upstream, *, emit_transcript=False):
    return {
        "harness": "opencode",
        "requiredEnv": ["FAKE_KEY"],
        "config": {
            "model": "fake/main",
            "standalone": True,
            "emitTranscript": emit_transcript,
            "providerDef": {
                "id": "fake",
                "npm": "@ai-sdk/openai-compatible",
                "baseUrl": upstream,
                "apiKeyEnv": "FAKE_KEY",
            },
            "opencodeConfig": {
                "provider": {
                    "fake": {
                        "models": {
                            model: {
                                "name": model,
                                "limit": {"context": 32768, "output": 2048},
                                "capabilities": {"tools": True},
                            }
                            for model in ("main", "worker")
                        }
                    }
                },
                "snapshots": False,
                "policies": [
                    {"action": "provider.use", "resource": "*", "effect": "deny"},
                    {"action": "provider.use", "resource": "fake", "effect": "allow"},
                ],
                "agents": {
                    "local-main": {
                        "mode": "primary",
                        "model": "fake/main",
                        "system": "MAIN-ONLY",
                        "permissions": [
                            {"action": "*", "resource": "*", "effect": "deny"},
                            {"action": "subagent", "resource": "local-worker", "effect": "allow"},
                        ],
                    },
                    "local-worker": {
                        "mode": "subagent",
                        "model": "fake/worker",
                        "description": "Local fake provider fixture, no real model",
                        "system": "WORKER-ONLY",
                        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
                    },
                },
            },
        },
        "failover": {
            "detect": {"status": [429, 402], "messages": ["usage limit"]},
            "vision": {},
            "wait": {"maxWaitHours": 8, "probeSeconds": 1},
        },
    }


def sse_text(text):
    return sse_frames({"content": text}, "stop")


def native_launcher_config():
    config = launcher_config("http://127.0.0.1:1")
    settings = config["config"]
    settings.pop("providerDef")
    settings["model"] = "openai/gpt-6.1-sol"
    native = settings["opencodeConfig"]
    native["provider"] = {"openai": {"options": {"apiKey": "FAKE", "transport": "http"}}}
    native["policies"][-1]["resource"] = "openai"
    for agent in native["agents"].values():
        agent["model"] = "openai/gpt-6.1-sol"
    config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
    return config


def sse_frames(delta, finish):
    frames = [
        {"id": "local", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"id": "local", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    return b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + (
        b"data: [DONE]\n\n"
    )


class RecoveryUpstream:
    def __init__(
        self,
        status,
        *,
        wall_message="temporary rate limit",
        persistent=False,
        worker_error=False,
        retry_after="1",
        wall_error=None,
        continuation_error=False,
    ):
        self.status = status
        self.requests = []
        self.times = []
        self.agents = []
        self.admitted = set()
        self.frames = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(body)
                fixture.times.append(time.monotonic())
                chat = responses_to_chat_request(body) if "input" in body else body
                system = "\n".join(
                    message["content"]
                    for message in chat["messages"]
                    if message["role"] == "system"
                )
                if "MAIN-ONLY" in system:
                    agent = "main"
                elif "WORKER-ONLY" in system:
                    agent = "worker"
                else:
                    agent = "auxiliary"
                fixture.agents.append(agent)
                has_tool_result = any(message["role"] == "tool" for message in chat["messages"])
                wall_key = (
                    "main-continuation"
                    if agent == "main" and has_tool_result and continuation_error
                    else agent
                )
                if agent != "auxiliary" and (persistent or wall_key not in fixture.admitted):
                    fixture.admitted.add(wall_key)
                    message = "usage limit" if worker_error and agent == "worker" else wall_message
                    payload = json.dumps({"error": wall_error or {"message": message}}).encode()
                    self.send_response(fixture.status)
                    self.send_header("Content-Type", "application/json")
                    if fixture.status in (400, 429) and retry_after is not None:
                        self.send_header(
                            "Retry-After", retry_after() if callable(retry_after) else retry_after
                        )
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if agent == "main" and not has_tool_result:
                        tool = next(
                            entry["function"]["name"]
                            for entry in chat.get("tools", [])
                            if entry["function"]["name"] == "subagent"
                        )
                        payload = sse_frames(
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "fake-child",
                                        "type": "function",
                                        "function": {
                                            "name": tool,
                                            "arguments": json.dumps(
                                                {
                                                    "agent": "local-worker",
                                                    "description": "Local recovery fixture",
                                                    "prompt": "WORKER-PROMPT",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                            "tool_calls",
                        )
                    else:
                        payload = sse_text(f"LOCAL-{agent.upper()}-OK")
                    if "input" in body:
                        payload = b"".join(
                            translate_chat_stream(io.BytesIO(payload).read, model=body["model"])
                        )
                    fixture.frames.append(payload)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def isolated_environment(root):
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        **{
            f"XDG_{name}_HOME": str(root / name.lower())
            for name in ("CONFIG", "CACHE", "DATA", "STATE")
        },
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_FILEWATCHER": "1",
    }


def run_installed(config, root, *, trailing_plugin=None):
    with _maybe_failover_proxy(config, {"FAKE_KEY": "fake"}) as plan:
        launch = build_launch(config, {"FAKE_KEY": "fake"}, failover=plan)
        environment = {**isolated_environment(root), **launch.env}
        if trailing_plugin is not None:
            document = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
            document.setdefault("plugins", []).append(str(trailing_plugin))
            environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(document)
        # The parent owns the ephemeral capability; only this isolated child inherits it.
        if plan is not None:
            environment[plan.capability_env] = os.environ[plan.capability_env]
        return subprocess.run(
            with_prompt(
                launch,
                ["--agent", "local-main", "--format", "json"],
                "MAIN-PROMPT",
                interactive=False,
            ),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
@pytest.mark.parametrize(
    "wall_status,wall_message,explicit",
    [
        (429, "temporary rate limit", False),
        (402, "temporary rate limit", False),
        (429, "usage limit", True),
        (429, "temporary rate limit", True),
    ],
)
def test_installed_v2_loader_main_worker_recovery_and_auxiliary_exclusion(
    tmp_path,
    wall_status,
    wall_message,
    explicit,
):
    with RecoveryUpstream(wall_status, wall_message=wall_message) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        if explicit:
            config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
        # These test plugins prove V2 unions both config spellings without moving user entries.
        plugin_paths = []
        for name in ("legacy", "native"):
            package = tmp_path / name
            package.mkdir()
            (package / "package.json").write_text(
                json.dumps(
                    {"name": f"test-{name}", "type": "module", "exports": {".": "./index.js"}}
                )
            )
            (package / "index.js").write_text(
                "export default {id: 'test."
                + name
                + "', async setup(ctx) {await ctx.session.hook('context', event => "
                + f"event.system.push({{type:'text',text:'USER-PLUGIN-{name}'}}));"
                + "}};"
            )
            plugin_paths.append(str(package))
        config["config"]["opencodeConfig"].update(
            plugin=[plugin_paths[0]], plugins=[plugin_paths[1]]
        )
        telemetry = policy_plugin(tmp_path)
        result = run_installed(config, tmp_path, trailing_plugin=telemetry)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "LOCAL-MAIN-OK" in result.stdout, result.stdout + result.stderr
        assert "LOCAL-WORKER-OK" in result.stdout, result.stdout + result.stderr
        main_requests = []
        worker_requests = []
        auxiliary_requests = []
        for body in upstream.requests:
            system = "\n".join(
                message["content"] for message in body["messages"] if message["role"] == "system"
            )
            if "MAIN-ONLY" in system or "WORKER-ONLY" in system:
                assert system.count(REMINDER) == 1
                assert system.count("USER-PLUGIN-legacy") == 1
                assert system.count("USER-PLUGIN-native") == 1
                (main_requests if "MAIN-ONLY" in system else worker_requests).append(body)
            else:
                assert REMINDER not in system
                auxiliary_requests.append(body)
        assert len(main_requests) == 3  # rejected admission, retry, tool-result continuation
        assert len(worker_requests) == 2  # rejected admission, retry
        assert main_requests[0] == main_requests[1]  # no extra prompt or retry-specific mutation
        assert worker_requests[0] == worker_requests[1]
        assert any(message["content"] == "MAIN-PROMPT" for message in main_requests[0]["messages"])
        assert (
            sum(
                message["content"].count("WORKER-PROMPT")
                for message in worker_requests[0]["messages"]
                if message["role"] == "user"
            )
            == 1
        )
        assert auxiliary_requests  # the real title flow must not receive the reminder
        assert (
            sum(message["role"] == "tool" for body in main_requests for message in body["messages"])
            == 1
        )
        if explicit:
            decisions = read_policy_events(tmp_path)
            assert len(decisions) == 2
            assert all(event["decision"] == {"retry": True, "delay": 1000} for event in decisions)
            for model in ("main", "worker"):
                timestamps = [
                    stamp
                    for body, stamp in zip(upstream.requests, upstream.times, strict=True)
                    if body["model"] == model
                    and any("-ONLY" in message["content"] for message in body["messages"])
                ]
                assert 0.9 <= timestamps[1] - timestamps[0] < 3, timestamps
        print(
            f"V2 localhost {wall_status}: main={len(main_requests)} worker={len(worker_requests)} "
            f"auxiliary={len(auxiliary_requests)}; "
            "one reminder, identical retries, original prompts"
        )


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
@pytest.mark.parametrize(
    "wall_status,wall_message,explicit",
    [
        (429, "usage limit", False),
        (429, "temporary rate limit", False),
        (429, "usage limit", True),
        (429, "temporary rate limit", True),
        (429, "usage limit: insufficient balance", True),
        (429, "usage limit: authentication failed", True),
        (402, "usage limit", True),
        (401, "usage limit", True),
        (403, "usage limit", True),
    ],
)
def test_installed_v2_terminal_or_exhausted_wall_is_not_hidden(
    tmp_path, wall_status, wall_message, explicit
):
    with RecoveryUpstream(wall_status, wall_message=wall_message, persistent=True) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        if explicit:
            config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
        # An explicit test-only veto replaces the brittle native-cap/90s-timeout assertion.
        # This measures hook ordering and finite fixture policy, not OpenCode's native cap.
        result = run_installed(config, tmp_path, trailing_plugin=policy_plugin(tmp_path, veto=3))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "LOCAL-MAIN-OK" not in result.stdout
    assert "LOCAL-WORKER-OK" not in result.stdout
    errors = [json.loads(line) for line in result.stdout.splitlines()]
    errors = [event["error"] for event in errors if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["message"] == wall_message
    requests = [
        body
        for body in upstream.requests
        if any("MAIN-ONLY" in message["content"] for message in body["messages"])
    ]
    assert requests
    assert all(body == requests[0] for body in requests)
    assert REMINDER in requests[0]["messages"][0]["content"]
    events = read_policy_events(tmp_path)
    if wall_status != 429 or (wall_message == "usage limit" and not explicit):
        assert len(requests) == 1
        assert events[0]["decision"] == {"retry": False}
    elif wall_message in ("usage limit", "temporary rate limit") and explicit:
        assert len(requests) == 2
        assert all(event["decision"] == {"retry": True, "delay": 1000} for event in events)
        assert events[-1]["attempt"] == 3
    elif "balance" in wall_message or "authentication" in wall_message:
        assert len(requests) == 1
        assert events[0]["decision"] == {"retry": False}
    else:
        assert len(requests) == 2
        assert events[-1]["attempt"] == 3
    print(f"V2 localhost {wall_message!r}: {len(requests)} main attempts, exit 1")


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
@pytest.mark.parametrize("style", ["rfc850", "asctime"])
def test_installed_v2_obsolete_http_date_main_and_worker_recovery(tmp_path, style):
    def next_reset():
        when = datetime.now(UTC) + timedelta(seconds=2)
        if style == "rfc850":
            return when.strftime("%A, %d-%b-%y %H:%M:%S GMT")
        return when.strftime("%a %b ") + f"{when.day:2d}" + when.strftime(" %H:%M:%S %Y")

    with RecoveryUpstream(429, wall_message="usage limit", retry_after=next_reset) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
        result = run_installed(config, tmp_path, trailing_plugin=policy_plugin(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCAL-MAIN-OK" in result.stdout
    assert "LOCAL-WORKER-OK" in result.stdout
    decisions = read_policy_events(tmp_path)
    assert len(decisions) == 2
    assert all(
        event["decision"]["retry"] is True and 0 < event["decision"]["delay"] <= 2000
        for event in decisions
    )
    main = [
        body
        for body in upstream.requests
        if any(
            "MAIN-ONLY" in message["content"]
            for message in body["messages"]
            if message["role"] == "system"
        )
    ]
    worker = [
        body
        for body in upstream.requests
        if any(
            "WORKER-ONLY" in message["content"]
            for message in body["messages"]
            if message["role"] == "system"
        )
    ]
    assert len(main) == 3 and main[0] == main[1]
    assert len(worker) == 2 and worker[0] == worker[1]
    assert sum(message["role"] == "tool" for body in main for message in body["messages"]) == 1


def policy_plugin(root, *, veto=None, cancel=False):
    package = root / "retry-policy-probe"
    package.mkdir()
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "local-retry-policy-probe",
                "type": "module",
                "exports": {".": "./index.js"},
            }
        )
    )
    log = root / "retry-events.jsonl"
    (package / "index.js").write_text(
        "import {appendFile} from 'node:fs/promises';"
        "export default {id:'local.retry-policy',async setup(ctx){"
        "await ctx.session.hook('retry',async event=>{"
        f"await appendFile({json.dumps(str(log))},JSON.stringify(event)+'\\n');"
        + (f"if(event.attempt >= {veto}) event.decision={{retry:false}};" if veto else "")
        + (
            "setTimeout(()=>ctx.session.interrupt({sessionID:event.sessionID,resume:false}),100);"
            if cancel
            else ""
        )
        + "});}};"
    )
    return package


def read_policy_events(root):
    return [json.loads(line) for line in (root / "retry-events.jsonl").read_text().splitlines()]


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for installed V2 localhost integration",
)
@pytest.mark.parametrize("status", [400, 429])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("retry_after", [None, "1"])
def test_installed_v2_subscription_http_main_worker_recovery(
    tmp_path, monkeypatch, status, native, retry_after
):
    error = {
        "code": SUBSCRIPTION_CODE,
        "type": "invalid_request_error",
        "message": SUBSCRIPTION_MESSAGE,
    }
    with RecoveryUpstream(
        status, wall_error=error, retry_after=retry_after, continuation_error=True
    ) as upstream:
        url = f"http://127.0.0.1:{upstream.server.server_port}/v1"
        config = native_launcher_config() if native else launcher_config(url)
        monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", url)
        monkeypatch.setattr("agedum.provider.OPENAI_CODEX_UPSTREAM", url)
        config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
        result = run_installed(config, tmp_path, trailing_plugin=policy_plugin(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCAL-MAIN-OK" in result.stdout
    assert "LOCAL-WORKER-OK" in result.stdout
    for agent, expected in (("main", 4), ("worker", 2)):
        calls = [
            (body, stamp)
            for body, stamp, owner in zip(
                upstream.requests, upstream.times, upstream.agents, strict=True
            )
            if owner == agent
        ]
        assert len(calls) == expected
        assert calls[0][0] == calls[1][0]
        assert 0.9 <= calls[1][1] - calls[0][1] < 3
        if agent == "main":
            assert calls[2][0] == calls[3][0]
            assert 0.9 <= calls[3][1] - calls[2][1] < 3
    main = [
        responses_to_chat_request(body) if "input" in body else body
        for body, owner in zip(upstream.requests, upstream.agents, strict=True)
        if owner == "main"
    ]
    assert [sum(message["role"] == "tool" for message in body["messages"]) for body in main] == [
        0,
        0,
        1,
        1,
    ]
    assert sum(b'"name": "subagent"' in frame for frame in upstream.frames) == 1
    events = read_policy_events(tmp_path)
    assert len(events) == 3
    assert all(event["error"]["type"] == "provider.chatgpt-limit" for event in events)
    assert all(event["error"]["status"] == 429 for event in events)
    assert all(event["decision"] == {"retry": True, "delay": 1000} for event in events)


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for installed V2 localhost integration",
)
@pytest.mark.parametrize(
    "status,mode",
    [
        (status, mode)
        for status in (400, 429)
        for mode in (
            "opt-out",
            "no-phrases",
            "unselected-phrase",
            "undetected",
            "beyond-cap",
            "billing",
            "authentication",
            "other-invalid",
            "unknown-code",
            "competing-code",
        )
    ]
    + [(status, "other-status") for status in (401, 402, 403, 422)],
)
def test_installed_v2_subscription_http_exclusions(tmp_path, monkeypatch, status, mode):
    error = {
        "code": SUBSCRIPTION_CODE,
        "type": "invalid_request_error",
        "message": SUBSCRIPTION_MESSAGE,
    }
    if mode in ("billing", "authentication", "other-invalid"):
        error["detail"] = {
            "billing": "billing required",
            "authentication": "invalid credentials",
            "other-invalid": "invalid configuration",
        }[mode]
    elif mode == "unknown-code":
        error["code"] = "other_usage_limit_exceeded"
    elif mode == "competing-code":
        error["additional_error"] = {"code": "unknown"}
    with RecoveryUpstream(status, wall_error=error, persistent=True, retry_after="2") as upstream:
        url = f"http://127.0.0.1:{upstream.server.server_port}/v1"
        monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", url)
        monkeypatch.setattr("agedum.provider.OPENAI_CODEX_UPSTREAM", url)
        config = native_launcher_config()
        if mode == "opt-out":
            config.pop("failover")
            config["config"]["opencodeConfig"]["provider"]["openai"]["options"]["baseURL"] = url
        elif mode == "no-phrases":
            config["failover"]["wait"].pop("retryMessages")
        elif mode == "unselected-phrase":
            config["failover"]["wait"]["retryMessages"] = ["temporary unavailable"]
        elif mode == "undetected":
            config["failover"]["detect"] = {"status": [418], "messages": ["unmatched"]}
        elif mode == "beyond-cap":
            config["failover"]["wait"]["maxWaitHours"] = 1 / 3600
        result = run_installed(config, tmp_path, trailing_plugin=policy_plugin(tmp_path, veto=3))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "LOCAL-MAIN-OK" not in result.stdout
    assert "LOCAL-WORKER-OK" not in result.stdout
    assert upstream.agents.count("main") == 1
    assert "worker" not in upstream.agents
    events = read_policy_events(tmp_path)
    assert len(events) == 1
    assert events[0]["decision"] == {"retry": False}


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for installed V2 localhost integration",
)
@pytest.mark.parametrize("status", [400, 429])
@pytest.mark.parametrize("cancel", [False, True])
def test_installed_v2_subscription_http_veto_and_cancellation(
    tmp_path, monkeypatch, status, cancel
):
    error = {
        "code": SUBSCRIPTION_CODE,
        "type": "invalid_request_error",
        "message": SUBSCRIPTION_MESSAGE,
    }
    with RecoveryUpstream(
        status, wall_error=error, persistent=True, retry_after="5" if cancel else "1"
    ) as upstream:
        url = f"http://127.0.0.1:{upstream.server.server_port}/v1"
        monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", url)
        monkeypatch.setattr("agedum.provider.OPENAI_CODEX_UPSTREAM", url)
        started = time.monotonic()
        result = run_installed(
            native_launcher_config(),
            tmp_path,
            trailing_plugin=policy_plugin(tmp_path, veto=None if cancel else 3, cancel=cancel),
        )
        events = read_policy_events(tmp_path)
        if cancel:
            assert time.monotonic() - started < 5
            assert len(events) == 1
            assert events[0]["decision"] == {"retry": True, "delay": 5000}
            time.sleep(5.1)
            assert upstream.agents.count("main") == 1
        else:
            assert result.returncode == 1
            assert len(events) == 2
            assert events[-1]["attempt"] == 3
            assert all(event["decision"] == {"retry": True, "delay": 1000} for event in events)
            assert upstream.agents.count("main") == 2
        assert "worker" not in upstream.agents
        assert "LOCAL-MAIN-OK" not in result.stdout


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for installed V2 localhost integration",
)
@pytest.mark.parametrize("kind", ["response.failed", "error", "http400"])
def test_installed_v2_subscription_native_transport_current_boundary(tmp_path, monkeypatch, kind):
    frame = {
        "type": "response.failed",
        "response": {
            "id": "local-failed",
            "status": "failed",
            "error": {"code": SUBSCRIPTION_CODE, "message": SUBSCRIPTION_MESSAGE},
        },
    }
    if kind == "error":
        frame = {"type": "error", "code": SUBSCRIPTION_CODE, "message": SUBSCRIPTION_MESSAGE}
    payload = f"event: {kind}\ndata: ".encode() + json.dumps(frame).encode() + b"\n\n"
    status, content_type = 200, "text/event-stream"
    if kind == "http400":
        frame = {
            "error": {
                "code": SUBSCRIPTION_CODE,
                "message": SUBSCRIPTION_MESSAGE,
                "type": "invalid_request_error",
            }
        }
        payload = json.dumps(frame).encode()
        status, content_type = 400, "application/json"
    config = {
        "harness": "opencode",
        "config": {
            "model": "openai/gpt-6.1-sol",
            "standalone": True,
            "emitTranscript": False,
            "opencodeConfig": {
                "snapshots": False,
                "provider": {"openai": {"options": {"apiKey": "FAKE", "transport": "http"}}},
                "agents": {"local-main": {"mode": "primary", "model": "openai/gpt-6.1-sol"}},
                "policies": [
                    {"action": "provider.use", "resource": "*", "effect": "deny"},
                    {"action": "provider.use", "resource": "openai", "effect": "allow"},
                ],
            },
        },
        "failover": {
            "detect": {"status": [429], "messages": ["quota"]},
            "vision": {},
            "wait": {
                "maxWaitHours": 8,
                "probeSeconds": 1,
                "retryMessages": ["usage limit", "rate limit"],
            },
        },
    }
    with Upstream(status=status, body=payload, content_type=content_type) as upstream:
        monkeypatch.setattr("agedum.proxy.OPENAI_API_UPSTREAM", upstream.url + "/v1")
        monkeypatch.setattr("agedum.provider.OPENAI_CODEX_UPSTREAM", upstream.url + "/codex")
        result = run_installed(config, tmp_path, trailing_plugin=policy_plugin(tmp_path, veto=3))
    assert result.returncode == 1, result.stdout + result.stderr
    assert SUBSCRIPTION_CODE in result.stdout, result.stdout + result.stderr
    events = read_policy_events(tmp_path)
    assert all(event["error"]["type"] == "provider.chatgpt-limit" for event in events)
    assert all(event["error"]["status"] == status for event in events)
    if kind == "http400":
        assert len(events) == 1
        assert events[0]["decision"] == {"retry": False}
        assert (
            "AI.Error.InvalidRequest" in (tmp_path / "data/opencode/log/opencode.log").read_text()
        )
    else:
        assert len(events) == 2
        assert all(event["decision"]["retry"] is True for event in events)
        assert events[-1]["attempt"] == 3  # Test hook veto, not a measured native maximum.
    assert json.loads(events[0]["error"]["response"]["body"]) == frame


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
def test_installed_v2_same_key_auxiliary_overlap_disables_retry_override(tmp_path):
    plugin = policy_plugin(tmp_path)
    log_path = json.dumps(str(tmp_path / "retry-events.jsonl"))
    (plugin / "index.js").write_text(
        "import {appendFile} from 'node:fs/promises';"
        "export default {id:'local.overlap',async setup(ctx){let generated=false;"
        f"const log=event=>appendFile({log_path},JSON.stringify(event)+'\\n');"
        "const identity=event=>({sessionID:event.sessionID,agent:event.agent,model:event.model});"
        "await ctx.session.hook('http.request',async event=>{"
        "await log({hook:'request',...identity(event),kind:event.kind});"
        "if(event.kind==='primary'&&!generated){generated=true;"
        "try{await ctx.session.generate({sessionID:event.sessionID,prompt:'AUXILIARY-PROMPT'});}"
        "catch(error){await log({hook:'generate-error',message:String(error)});}}});"
        "await ctx.session.hook('http.response',event=>log({hook:'response',...identity(event),"
        "kind:event.kind,status:event.response.status,"
        "marker:event.response.headers.get('x-agedum-retry-eligible')}));"
        "await ctx.session.hook('retry',event=>log({hook:'retry',...event}));}};"
    )
    with RecoveryUpstream(429, wall_message="usage limit", persistent=True) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        config["failover"]["wait"]["retryMessages"] = ["usage limit", "rate limit"]
        result = run_installed(config, tmp_path, trailing_plugin=plugin)
    assert result.returncode == 1, result.stdout + result.stderr
    events = read_policy_events(tmp_path)
    requests = [
        event
        for event in events
        if event["hook"] == "request" and event["kind"] in ("primary", "generate")
    ]
    assert [event["kind"] for event in requests] == ["primary", "generate"]
    assert requests[0]["sessionID"] == requests[1]["sessionID"]
    assert requests[0]["agent"] == requests[1]["agent"]
    assert requests[0]["model"] == requests[1]["model"]
    responses = [
        event
        for event in events
        if event["hook"] == "response" and event["kind"] in ("primary", "generate")
    ]
    assert [event["kind"] for event in responses] == ["generate", "primary"]
    assert all(event["status"] == 429 and event["marker"] == "1" for event in responses)
    retries = [event for event in events if event["hook"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["decision"] == {"retry": False}
    assert "usage limit" in result.stdout
    assert "LOCAL-MAIN-OK" not in result.stdout


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
def test_installed_v2_cancellation_during_accepted_backoff_stops_requests(tmp_path):
    with RecoveryUpstream(
        429, wall_message="usage limit", persistent=True, retry_after="5"
    ) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        config["failover"]["wait"]["retryMessages"] = ["usage limit"]
        started = time.monotonic()
        result = run_installed(
            config, tmp_path, trailing_plugin=policy_plugin(tmp_path, cancel=True)
        )
        assert time.monotonic() - started < 5
        assert "LOCAL-MAIN-OK" not in result.stdout
        assert read_policy_events(tmp_path)[0]["decision"] == {"retry": True, "delay": 5000}
        time.sleep(5.1)
        assert (
            len(
                [
                    body
                    for body in upstream.requests
                    if any("MAIN-ONLY" in message["content"] for message in body["messages"])
                ]
            )
            == 1
        )


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
def test_installed_v2_worker_failure_reaches_parent_without_replaying_task(tmp_path):
    with RecoveryUpstream(429, worker_error=True) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        result = run_installed(config, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCAL-WORKER-OK" not in result.stdout
    worker_requests = [body for body in upstream.requests if body["model"] == "worker"]
    assert len(worker_requests) == 1
    tool_results = [
        message
        for body in upstream.requests
        for message in body["messages"]
        if message["role"] == "tool"
    ]
    assert len(tool_results) == 1
    assert "usage limit" in json.dumps(tool_results[0]), tool_results
    assert "LOCAL-WORKER-OK" not in json.dumps(tool_results[0])
    assert REMINDER in worker_requests[0]["messages"][0]["content"]
    print("V2 localhost worker failure: one worker attempt, original error returned to parent")
