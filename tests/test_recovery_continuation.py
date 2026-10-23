import copy
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agedum.cli.main import _maybe_failover_proxy
from agedum.provider import FailoverPlan, build_launch, with_prompt

PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1] / "agedum/assets/opencode"
PLUGIN = PLUGIN_DIRECTORY / "recovery-continuation.js"
REMINDER = (
    "If this task resumes after a temporary provider-limit wait, continue the original task "
    "from its recorded state. Do not treat an earlier usage-limit error as a permanent block "
    "or as a review result. Reconcile unfinished worker work before retrying it; do not repeat "
    "completed side effects. This instruction does not authorize changing provider/model or "
    "bypassing permissions/review."
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
    assert "plugins" not in document


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
        self, status, *, wall_message="temporary rate limit", persistent=False, worker_error=False
    ):
        self.status = status
        self.requests = []
        self.admitted = set()
        self.frames = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(body)
                system = "\n".join(
                    message["content"]
                    for message in body["messages"]
                    if message["role"] == "system"
                )
                if "MAIN-ONLY" in system:
                    agent = "main"
                elif "WORKER-ONLY" in system:
                    agent = "worker"
                else:
                    agent = "auxiliary"
                if agent != "auxiliary" and (persistent or agent not in fixture.admitted):
                    fixture.admitted.add(agent)
                    message = "usage limit" if worker_error and agent == "worker" else wall_message
                    payload = json.dumps({"error": {"message": message}}).encode()
                    self.send_response(fixture.status)
                    self.send_header("Content-Type", "application/json")
                    if fixture.status == 429:
                        self.send_header("Retry-After", "1")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if agent == "main" and not any(
                        message["role"] == "tool" for message in body["messages"]
                    ):
                        tool = next(
                            entry["function"]["name"]
                            for entry in body.get("tools", [])
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


def run_installed(config, root):
    with _maybe_failover_proxy(config, {"FAKE_KEY": "fake"}) as plan:
        launch = build_launch(config, {"FAKE_KEY": "fake"}, failover=plan)
        environment = {**isolated_environment(root), **launch.env}
        # The parent owns the ephemeral capability; only this isolated child inherits it.
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
@pytest.mark.parametrize("wall_status", [429, 402])
def test_installed_v2_loader_main_worker_recovery_and_auxiliary_exclusion(tmp_path, wall_status):
    with RecoveryUpstream(wall_status) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
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
        result = run_installed(config, tmp_path)
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
        print(
            f"V2 localhost {wall_status}: main={len(main_requests)} worker={len(worker_requests)} "
            f"auxiliary={len(auxiliary_requests)}; "
            "one reminder, identical retries, original prompts"
        )


@pytest.mark.skipif(
    os.environ.get("AGEDUM_TEST_OPENCODE") != "1" or shutil.which("opencode") is None,
    reason="Set AGEDUM_TEST_OPENCODE=1 for the installed V2 localhost integration test",
)
@pytest.mark.parametrize("wall_message", ["usage limit", "temporary rate limit"])
def test_installed_v2_terminal_or_exhausted_wall_is_not_hidden(tmp_path, wall_message):
    with RecoveryUpstream(429, wall_message=wall_message, persistent=True) as upstream:
        config = launcher_config(f"http://127.0.0.1:{upstream.server.server_port}/v1")
        result = run_installed(config, tmp_path)
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
    if wall_message == "usage limit":
        assert errors[0]["type"] == "provider.quota"
        assert len(requests) == 1
    else:
        assert len(requests) > 1  # subprocess timeout bounds the unchanged native retry policy
    print(f"V2 localhost {wall_message!r}: {len(requests)} main attempts, exit 1")


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
