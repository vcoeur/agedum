"""Regression versions of the credential/stream audit's localhost reproductions."""

import http.client
import io
import json
import os
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from agedum import provider, proxy
from agedum.cli import main


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AGEDUM_TRANSLATE_OPENAI",
        "AGEDUM_FOLD_SYSTEM_MESSAGES",
        "AGEDUM_CODEX_CHAT_UPSTREAM",
        proxy.CAPABILITY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


class Upstream:
    def __init__(self, status=200, body=b'{"ok":true}', content_type="application/json"):
        self.requests = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def respond(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                state.requests.append((self.path, list(self.headers.items()), raw))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_POST = do_GET = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = respond

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def request(url, body=None, headers=(), method="POST"):
    address = urlsplit(url)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
    raw = json.dumps(body).encode() if body is not None else b""
    try:
        connection.putrequest(method, address.path or "/")
        connection.putheader("Content-Length", str(len(raw)))
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(raw)
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def route(url, key, openai=False):
    return {
        "upstream": url,
        "api_key": key,
        "openai": openai,
        "models": {"m": {"id": "m", "options": {}}},
        "keys_by_wire": {"m": ["m"]},
    }


def spec(primary, fallback):
    return {
        "routes": {"p": route(primary, "FAKE-PRIMARY"), "f": route(fallback, "FAKE-FALLBACK")},
        "status": [429, 402],
        "messages": ["quota"],
        "max_walk": 3,
        "chains": {"p/m": ("f/m",)},
        "vision": {"p/m": True, "f/m": True},
    }


def make_proxy(kind, upstream):
    if kind == "fold":
        return proxy.FoldProxy(upstream)
    if kind == "translate":
        return proxy.TranslateProxy(upstream, api_key="FAKE-PRIVATE")
    if kind == "responses":
        return proxy.ResponsesToChatProxy(upstream)
    return proxy.FailoverProxy(spec(upstream, upstream))


def path(kind):
    return "/oc/p/chat/completions" if kind == "failover" else "/v1/messages"


@pytest.mark.parametrize("kind", ["fold", "translate", "responses", "failover"])
@pytest.mark.parametrize("method", ["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_every_proxy_rejects_missing_capability_before_upstream(kind, method):
    with Upstream() as upstream, make_proxy(kind, upstream.url) as server:
        status, _, _ = request(server.base_url + path(kind), method=method)
        assert status == 401
        assert upstream.requests == []


@pytest.mark.parametrize("kind", ["fold", "translate", "responses", "failover"])
@pytest.mark.parametrize(
    "invalid", ["wrong", "upstream-key", "duplicate", "comma", "empty", "unicode"]
)
def test_capability_is_not_an_upstream_credential_or_duplicate(kind, invalid):
    with Upstream() as upstream, make_proxy(kind, upstream.url) as server:
        values = {
            "wrong": ["FAKE-WRONG"],
            "upstream-key": ["FAKE-PRIVATE"],
            "duplicate": [server.capability, server.capability],
            "comma": [server.capability + "," + server.capability],
            "empty": [""],
            "unicode": ["\xe9"],
        }[invalid]
        headers = [(proxy.CAPABILITY_HEADER, value) for value in values]
        status, _, raw = request(server.base_url + path(kind), {"model": "m"}, headers)
        assert status == 401
        assert server.capability.encode() not in raw
        assert upstream.requests == []


@pytest.mark.parametrize("kind", ["fold", "translate", "responses", "failover"])
def test_browser_origin_is_additional_defense_not_admission(kind):
    with Upstream() as upstream, make_proxy(kind, upstream.url) as server:
        status, _, raw = request(
            server.base_url + path(kind),
            {},
            [
                (proxy.CAPABILITY_HEADER, server.capability),
                ("Origin", "https://untrusted.invalid"),
            ],
        )
        assert status == 403
        assert server.capability.encode() not in raw
        assert upstream.requests == []


@pytest.mark.parametrize(
    "kind,local_path",
    [
        ("translate", "/v1/messages/count_tokens"),
        ("responses", "/models"),
    ],
)
def test_local_short_circuits_also_require_capability(kind, local_path):
    with make_proxy(kind, "http://127.0.0.1:1") as server:
        assert request(server.base_url + local_path, method="GET")[0] == 401
        assert (
            request(
                server.base_url + local_path,
                headers=[
                    (proxy.CAPABILITY_HEADER.lower(), server.capability),
                ],
                method="GET",
            )[0]
            == 200
        )


@pytest.mark.parametrize(
    "names",
    [
        ["Authorization"],
        ["authorization"],
        ["aUtHoRiZaTiOn"],
        ["Authorization", "authorization", "aUtHoRiZaTiOn", "Authorization"],
    ],
)
def test_fallback_never_receives_primary_auth_with_any_duplicate_casing(names, capsys):
    with Upstream(429, b"quota") as primary, Upstream() as fallback:
        with proxy.FailoverProxy(spec(primary.url, fallback.url)) as server:
            status, _, raw = request(
                server.base_url + "/oc/p/chat/completions",
                {
                    "model": "m",
                    "messages": [],
                },
                [
                    (proxy.CAPABILITY_HEADER, server.capability),
                    *[(name, "Bearer FAKE-PRIMARY") for name in names],
                    ("X-Api-Key", "FAKE-PRIMARY"),
                    ("api-key", "FAKE-PRIMARY"),
                    ("ChatGPT-Account-Id", "FAKE-ACCOUNT"),
                ],
            )
            capability = server.capability
    assert status == 200
    received = [value for name, value in fallback.requests[0][1] if name.lower() == "authorization"]
    assert received == ["Bearer FAKE-FALLBACK"]
    assert "FAKE-PRIMARY" not in repr(fallback.requests)
    assert "FAKE-ACCOUNT" not in repr(fallback.requests)
    assert capability not in repr(primary.requests + fallback.requests)
    captured = capsys.readouterr()
    assert capability not in captured.out + captured.err + raw.decode()


def events(raw):
    return [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]


def sse(*frames, done=True):
    raw = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
    return raw + (b"data: [DONE]\n\n" if done else b"")


@pytest.mark.parametrize(
    "finish,done,expected,reason",
    [
        (None, False, "incomplete", "upstream_eof"),
        (None, True, "incomplete", "upstream_eof"),
        ("stop", False, "incomplete", "upstream_eof"),
        ("stop", True, "completed", None),
        ("length", True, "incomplete", "max_output_tokens"),
        ("content_filter", True, "incomplete", "content_filter"),
        ("unknown", True, "failed", None),
    ],
)
def test_responses_terminal_reason_is_not_fabricated(finish, done, expected, reason):
    raw = sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": finish}]}, done=done)
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    terminal = result[-1]
    assert terminal["type"] == f"response.{expected}"
    assert terminal["response"]["status"] == expected
    if reason:
        assert terminal["response"]["incomplete_details"] == {"reason": reason}
    if expected != "completed":
        assert not any(frame["type"].endswith(".done") for frame in result)
        assert not any(frame["type"] == "response.completed" for frame in result)
        if finish == "unknown":
            assert terminal["response"]["output"] == []
            assert terminal["response"]["error"]["code"] == "invalid_upstream_stream"
            assert not any(frame["type"] == "response.output_text.delta" for frame in result)
        else:
            assert terminal["response"]["output"][0]["content"][0]["text"] == "partial"


@pytest.mark.parametrize(
    "raw",
    [
        sse({"error": {"message": "FAKE-SENSITIVE upstream error"}}),
        b"data: not-json\n\n",
        b'data: {"choices":',
        b"data: null\n\n",
        b"",
    ],
)
def test_responses_error_malformed_and_empty_streams_are_not_success(raw):
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] in ("response.failed", "response.incomplete")
    assert "FAKE-SENSITIVE" not in json.dumps(result)
    assert not any(frame["type"] == "response.completed" for frame in result)


def test_transport_failure_never_completes_partial_tool_call():
    calls = iter(
        [
            sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call",
                                        "function": {"name": "run", "arguments": '{"cmd":'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                done=False,
            )
        ]
    )

    def broken_read(size):
        try:
            return next(calls)
        except StopIteration:
            raise http.client.IncompleteRead(b"partial") from None

    result = events(b"".join(proxy.translate_chat_stream(broken_read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == "upstream_disconnect"
    assert not any("function_call" in frame["type"] for frame in result)


@pytest.mark.parametrize(
    "finish,arguments,expected",
    [
        ("tool_calls", '{"ok":true}', "completed"),
        ("function_call", "{}", "completed"),
        ("length", '{"ok":', "incomplete"),
        ("tool_calls", '{"ok":', "failed"),
        ("tool_calls", "[]", "failed"),
    ],
)
def test_unfinished_tool_arguments_never_become_done(finish, arguments, expected):
    raw = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "function": {"name": "run", "arguments": arguments},
                            }
                        ]
                    },
                    "finish_reason": finish,
                }
            ]
        }
    )
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == f"response.{expected}"
    assert any(frame["type"] == "response.function_call_arguments.done" for frame in result) == (
        expected == "completed"
    )


@pytest.mark.parametrize("translate", [False, True])
def test_claude_runtime_header_wiring_preserves_separate_upstream_auth(translate, monkeypatch):
    body = (
        b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
        if translate
        else b'{"ok":true}'
    )
    with Upstream(body=body) as upstream:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", upstream.url)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE-REAL-UPSTREAM")
        monkeypatch.setenv(
            "ANTHROPIC_CUSTOM_HEADERS", "X-Trace: retained\nx-agedum-proxy-capability: stale"
        )
        monkeypatch.setenv(
            "AGEDUM_TRANSLATE_OPENAI" if translate else "AGEDUM_FOLD_SYSTEM_MESSAGES", "1"
        )
        before = dict(os.environ)
        with main._maybe_proxy("claude"):
            headers = [
                tuple(line.split(": ", 1))
                for line in os.environ["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
            ]
            capability = dict(headers)[proxy.CAPABILITY_HEADER]
            assert capability != "FAKE-REAL-UPSTREAM"
            status, _, _ = request(
                os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
                {
                    "model": "m",
                    "messages": [
                        {"role": "system", "content": "context"},
                        {"role": "user", "content": "hi"},
                    ],
                },
                [*headers, ("X-Api-Key", os.environ["ANTHROPIC_API_KEY"])],
            )
            assert status == 200
        assert dict(os.environ) == before
    forwarded = dict(upstream.requests[0][1])
    assert (
        forwarded["Authorization" if translate else "X-Api-Key"]
        == ("Bearer " if translate else "") + "FAKE-REAL-UPSTREAM"
    )
    assert forwarded["X-Trace"] == "retained"
    assert capability not in repr(upstream.requests)


def test_codex_runtime_env_header_wiring_preserves_bearer_without_secret_argv(monkeypatch):
    raw = sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    with Upstream(body=raw, content_type="text/event-stream") as upstream:
        monkeypatch.setenv("AGEDUM_CODEX_CHAT_UPSTREAM", upstream.url)
        monkeypatch.setenv(proxy.CAPABILITY_ENV, "stale")
        command = ["codex", "-c", f'model_providers.agedum.base_url="{upstream.url}"']
        with main._maybe_codex_proxy("codex", command) as child_command:
            config = {}
            for index, arg in enumerate(child_command):
                if arg == "-c":
                    config.update(
                        tomllib.loads(child_command[index + 1])["model_providers"]["agedum"]
                    )
            capability = os.environ[config["env_http_headers"][proxy.CAPABILITY_HEADER]]
            assert capability not in " ".join(child_command)
            status, _, output = request(
                config["base_url"] + "/responses",
                {"model": "m", "input": "hello"},
                [
                    (proxy.CAPABILITY_HEADER, capability),
                    ("Authorization", "Bearer FAKE-UPSTREAM"),
                ],
            )
            assert status == 200
            assert events(output)[-1]["type"] == "response.completed"
        assert os.environ[proxy.CAPABILITY_ENV] == "stale"
    assert dict(upstream.requests[0][1])["Authorization"] == "Bearer FAKE-UPSTREAM"
    assert capability not in repr(upstream.requests)


@pytest.mark.parametrize(
    "raw",
    [
        sse({"choices": [{"delta": {"content": "partial"}}]}, done=False),
        sse({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        sse({"error": {"message": "generation failed"}}),
    ],
)
def test_responses_proxy_runtime_surfaces_failed_or_incomplete_stream(raw):
    with Upstream(body=raw, content_type="text/event-stream") as upstream:
        with proxy.ResponsesToChatProxy(upstream.url) as server:
            status, _, output = request(
                server.base_url + "/responses",
                {"model": "m", "input": "hello"},
                [
                    (proxy.CAPABILITY_HEADER, server.capability),
                ],
            )
    assert status == 200
    assert events(output)[-1]["type"] in ("response.incomplete", "response.failed")


@pytest.mark.parametrize("token", ['FAKE-QUOTE"SLASH\\TAIL', "FAKE-\n\t\r\x01", "FAKE-秘密-é", "1"])
def test_typed_redaction_preserves_json_toml_argv_env_and_unicode(token, capsys):
    prompt = "Unicode prompt 你好 café 1 with model-1 and 1000 tokens"
    launch = provider.build_launch(
        {
            "harness": "opencode",
            "requiredEnv": ["TOKEN", "SWITCH"],
            "config": {
                "providerDef": {
                    "id": "p",
                    "npm": "@ai-sdk/openai-compatible",
                    "baseUrl": "http://127.0.0.1:1",
                    "apiKeyEnv": "TOKEN",
                },
                "opencodeConfig": {"agent": {"worker": {"prompt": prompt}}, "numeric": 1},
            },
        },
        {"TOKEN": token, "SWITCH": "1"},
    )
    original = launch.env["OPENCODE_CONFIG_CONTENT"]
    main._print_environment(launch)
    output = capsys.readouterr().out
    document = output.split("  OPENCODE_CONFIG_CONTENT\n", 1)[1]
    redacted = json.loads(
        "\n".join(line[4:] for line in document.splitlines() if line.startswith("    "))
    )
    assert redacted["provider"]["p"]["options"]["apiKey"] == "***"
    assert redacted["agent"]["worker"]["prompt"] == prompt
    assert redacted["numeric"] == 1
    assert launch.env["OPENCODE_CONFIG_CONTENT"] == original
    toml = (
        "api_key = "
        + provider._toml_config_value(token)
        + "\nprompt = "
        + provider._toml_config_value(prompt)
        + "\ncount = 1\n"
    )
    parsed = tomllib.loads(main._diagnostic_document(toml, [token, "1"], ".toml"))
    assert parsed == {"api_key": "***", "prompt": prompt, "count": 1}
    argv = main._diagnostic_argv(
        [
            "codex",
            "-c",
            "auth.token=" + provider._toml_config_value(token),
            "-c",
            "prompt=" + provider._toml_config_value(prompt),
            "--settings",
            json.dumps({"apiKey": token, "prompt": prompt, "count": 1}),
            "--key",
            token,
            "--run",
            prompt,
        ],
        [token, "1"],
    )
    assert tomllib.loads(argv[2])["auth"]["token"] == "***"
    assert tomllib.loads(argv[4])["prompt"] == prompt
    assert json.loads(argv[6]) == {"apiKey": "***", "prompt": prompt, "count": 1}
    assert argv[8] == "***" and argv[-1] == prompt
    if token != "1":
        assert token not in output + " ".join(argv)


def test_unparseable_diagnostics_fail_closed():
    for suffix in (".json", ".toml", ".unknown"):
        output = main._diagnostic_document('bad FAKE-SECRET"\\', ["FAKE-SECRET"], suffix)
        assert "FAKE-SECRET" not in output


@pytest.mark.parametrize(
    "chunk",
    [
        {"choices": {}},
        {"choices": "not-a-list"},
        {"choices": [None]},
        {"choices": [{"delta": "not-a-dict"}]},
        {"choices": [{"delta": {"tool_calls": [None]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": -1}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": "not-a-dict"}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": 1}}]}}]},
    ],
)
def test_malformed_chat_structures_cannot_report_success(chunk):
    raw = sse(chunk)
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_upstream_stream"


_MALFORMED_FALSY_VALUES = [{}, "", False, None, 0, 0.0]


def malformed_container_frame(field, value):
    if field == "choices":
        return {"choices": value}
    if field == "delta":
        return {"choices": [{"delta": value}]}
    if field == "tool_calls":
        return {"choices": [{"delta": {"tool_calls": value}}]}
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": value}]}}]}


@pytest.mark.parametrize(
    "field,value",
    [
        *[
            (field, value)
            for field in ("choices", "tool_calls")
            for value in _MALFORMED_FALSY_VALUES
            if field == "choices" or value is not None
        ],
        *[
            (field, value)
            for field in ("delta", "function")
            for value in ("", False, None, 0, 0.0, [])
            if field == "delta" or value is not None
        ],
    ],
)
def test_malformed_present_container_after_stop_is_failed(field, value):
    raw = sse(
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]},
        malformed_container_frame(field, value),
    )
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["status"] == "failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_upstream_stream"
    assert not any(frame["type"] == "response.completed" for frame in result)
    assert not any(frame["type"] == "response.function_call_arguments.done" for frame in result)


@pytest.mark.parametrize("value", ["", False, 0, 0.0, []])
def test_malformed_function_after_successful_tool_finish_is_failed(value):
    raw = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "function": {"name": "run", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        malformed_container_frame("function", value),
    )
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_upstream_stream"
    assert not any(frame["type"] == "response.function_call_arguments.done" for frame in result)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        {"choices": []},
        {"choices": [], "usage": {"completion_tokens": 1}},
    ],
)
def test_absent_choices_and_empty_choice_metadata_after_stop_still_complete(metadata):
    raw = sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        metadata,
    )
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.completed"


@pytest.mark.parametrize("index", [False, True])
def test_boolean_tool_index_after_stop_is_a_malformed_frame(index):
    raw = sse(
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": "call",
                                "function": {"name": "run", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
    )
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_upstream_stream"


@pytest.mark.parametrize("kind", ["responses", "failover"])
@pytest.mark.parametrize("value", _MALFORMED_FALSY_VALUES)
def test_malformed_choices_after_stop_fail_on_both_runtime_routes(kind, value):
    raw = sse(
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]},
        {"choices": value},
    )
    with Upstream(429, b"quota") as primary:
        with Upstream(body=raw, content_type="text/event-stream") as upstream:
            server = (
                proxy.ResponsesToChatProxy(upstream.url)
                if kind == "responses"
                else proxy.FailoverProxy(spec(primary.url, upstream.url))
            )
            with server:
                local_path = "/responses" if kind == "responses" else "/oc/p/responses"
                status, _, output = request(
                    server.base_url + local_path,
                    {"model": "m", "input": "hello"},
                    [(proxy.CAPABILITY_HEADER, server.capability)],
                )
    assert status == 200
    result = events(output)
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_upstream_stream"
    assert not any(frame["type"] == "response.completed" for frame in result)


_BAD_CHAT_TYPES = [False, True, 0, 1, -1, 0.0, 1.25, [], {}, [None]]
_BAD_CHAT_INDEX = [False, True, -1, None, 0.0, 1.25, "", [], {}]
_BAD_CHAT_LIST = [None, False, True, 0, -1, 0.0, "", {}, "list"]
_BAD_CHAT_OBJECT = [None, False, True, 0, -1, 0.0, "", [], "object"]
_CHAT_CHOICE = ("choices", 0)
_CHAT_DELTA = (*_CHAT_CHOICE, "delta")
_CHAT_TOOL = (*_CHAT_DELTA, "tool_calls", 0)
_CHAT_FUNCTION = (*_CHAT_TOOL, "function")
_CHAT_LEGACY_FUNCTION = (*_CHAT_DELTA, "function_call")

# Each row owns invalid wire values and valid metadata controls independently of runtime rules.
_CHAT_FIELD_MATRIX = [
    (("choices",), _BAD_CHAT_LIST, [[]]),
    (("id",), [None, *_BAD_CHAT_TYPES], ["", "upstream-id"]),
    (("model",), [None, *_BAD_CHAT_TYPES], ["", "m"]),
    (("created",), _BAD_CHAT_INDEX, [0, 1]),
    (("object",), [None, *_BAD_CHAT_TYPES, "", "unknown"], ["chat.completion.chunk"]),
    (("usage",), [item for item in _BAD_CHAT_OBJECT if item is not None], [None, {}]),
    *(
        (("usage", key), _BAD_CHAT_INDEX, [0, 1])
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        )
    ),
    (_CHAT_CHOICE, _BAD_CHAT_OBJECT, [{}]),
    ((*_CHAT_CHOICE, "index"), _BAD_CHAT_INDEX, [0, 1]),
    ((*_CHAT_CHOICE, "finish_reason"), [*_BAD_CHAT_TYPES, "", "unknown"], [None, "stop"]),
    (_CHAT_DELTA, _BAD_CHAT_OBJECT, [{}]),
    *(
        ((*_CHAT_DELTA, key), _BAD_CHAT_TYPES, [None, ""])
        for key in (
            "content",
            "reasoning_content",
            "refusal",
        )
    ),
    (
        (*_CHAT_DELTA, "role"),
        [*_BAD_CHAT_TYPES, "", "unknown"],
        [
            None,
            "developer",
            "system",
            "user",
            "assistant",
            "tool",
        ],
    ),
    (
        (*_CHAT_DELTA, "tool_calls"),
        [item for item in _BAD_CHAT_LIST if item is not None],
        [None, []],
    ),
    (_CHAT_LEGACY_FUNCTION, [item for item in _BAD_CHAT_OBJECT if item is not None], [None, {}]),
    (_CHAT_TOOL, _BAD_CHAT_OBJECT, [{}]),
    ((*_CHAT_TOOL, "index"), _BAD_CHAT_INDEX, [0, 1]),
    ((*_CHAT_TOOL, "id"), _BAD_CHAT_TYPES, [None, "", "call"]),
    ((*_CHAT_TOOL, "type"), [*_BAD_CHAT_TYPES, "", "custom"], [None, "function"]),
    (_CHAT_FUNCTION, [item for item in _BAD_CHAT_OBJECT if item is not None], [None, {}]),
    ((*_CHAT_FUNCTION, "name"), _BAD_CHAT_TYPES, [None, "", "run"]),
    ((*_CHAT_FUNCTION, "arguments"), _BAD_CHAT_TYPES, [None, ""]),
    ((*_CHAT_LEGACY_FUNCTION, "name"), _BAD_CHAT_TYPES, [None, ""]),
    ((*_CHAT_LEGACY_FUNCTION, "arguments"), _BAD_CHAT_TYPES, [None, ""]),
]


def chat_field_frame(path, value):
    frame = {}
    if path[0] == "choices":
        frame = {"choices": [{"index": 0, "delta": {}}]}
        if "tool_calls" in path:
            frame["choices"][0]["delta"]["tool_calls"] = [{"index": 0, "function": {}}]
        if "function_call" in path:
            frame["choices"][0]["delta"]["function_call"] = {}
    elif path[0] == "usage":
        frame = {"usage": {}}
    current = frame
    for segment in path[:-1]:
        current = current[segment]
    current[path[-1]] = value
    return frame


def finished_chat_frame():
    return {
        "choices": [
            {
                "delta": {
                    "content": "partial",
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call-{index}",
                            "function": {"name": "run", "arguments": "{}"},
                        }
                        for index in (0, 1)
                    ],
                },
                "finish_reason": "stop",
            }
        ]
    }


def assert_malformed_chat_failed(result):
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["status"] == "failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_upstream_stream"
    assert not any(frame["type"] == "response.completed" for frame in result)
    assert not any(frame["type"] == "response.function_call_arguments.done" for frame in result)


@pytest.mark.parametrize(
    "path,value", [(path, value) for path, invalid, _ in _CHAT_FIELD_MATRIX for value in invalid]
)
def test_consumed_chat_field_matrix_fails_after_successful_finish(path, value):
    raw = sse(finished_chat_frame(), chat_field_frame(path, value))
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert_malformed_chat_failed(result)


@pytest.mark.parametrize(
    "path,value", [(path, value) for path, _, valid in _CHAT_FIELD_MATRIX for value in valid]
)
def test_consumed_chat_field_matrix_accepts_valid_metadata(path, value):
    raw = sse(finished_chat_frame(), chat_field_frame(path, value))
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.completed"
    assert result[-1]["response"]["output"][0]["content"][0]["text"] == "partial"


_CHAT_RUNTIME_BAD_FIELDS = (
    [(path, invalid[0]) for path, invalid, _ in _CHAT_FIELD_MATRIX]
    + [
        ((*_CHAT_DELTA, field), value)
        for field in ("content", "reasoning_content")
        for value in (0, [], {})
    ]
    + [
        (path, value)
        for path in ((*_CHAT_CHOICE, "index"), (*_CHAT_TOOL, "index"))
        for value in (-1, True)
    ]
)


@pytest.mark.parametrize("kind", ["responses", "failover"])
@pytest.mark.parametrize("path,value", _CHAT_RUNTIME_BAD_FIELDS)
def test_consumed_chat_field_matrix_fails_on_authenticated_runtime_routes(kind, path, value):
    raw = sse(finished_chat_frame(), chat_field_frame(path, value))
    with Upstream(429, b"quota") as primary:
        with Upstream(body=raw, content_type="text/event-stream") as upstream:
            server = (
                proxy.ResponsesToChatProxy(upstream.url)
                if kind == "responses"
                else proxy.FailoverProxy(spec(primary.url, upstream.url))
            )
            with server:
                local_path = "/responses" if kind == "responses" else "/oc/p/responses"
                status, _, output = request(
                    server.base_url + local_path,
                    {"model": "m", "input": "hello"},
                    [(proxy.CAPABILITY_HEADER, server.capability)],
                )
    assert status == 200
    assert_malformed_chat_failed(events(output))


@pytest.mark.parametrize("level", ["root", "choice", "delta", "tool", "function", "usage"])
@pytest.mark.parametrize("value", [False, 0, None, "", [], {}])
def test_unused_vendor_metadata_is_not_rejected(level, value):
    paths = {
        "root": ("vendor",),
        "choice": (*_CHAT_CHOICE, "vendor"),
        "delta": (*_CHAT_DELTA, "vendor"),
        "tool": (*_CHAT_TOOL, "vendor"),
        "function": (*_CHAT_FUNCTION, "vendor"),
        "usage": ("usage", "vendor"),
    }
    raw = sse(finished_chat_frame(), chat_field_frame(paths[level], value))
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.completed"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("refusal", "Cannot comply", "upstream_refusal"),
        ("function_call", {"name": "run", "arguments": "{}"}, "unsupported_function_call"),
    ],
)
def test_unsupported_output_is_a_failure_not_silently_dropped(field, value, code):
    raw = sse(finished_chat_frame(), chat_field_frame((*_CHAT_DELTA, field), value))
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == code
    assert not any(frame["type"] == "response.function_call_arguments.done" for frame in result)


@pytest.mark.parametrize("finish", ["tool_calls", "function_call"])
def test_tool_finish_without_any_tool_never_completes(finish):
    raw = sse({"choices": [{"delta": {}, "finish_reason": finish}]})
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["error"]["code"] == "invalid_tool_arguments"


def test_malformed_unselected_choice_is_still_a_malformed_chunk():
    raw = sse(
        finished_chat_frame(),
        {
            "choices": [
                {"delta": {}},
                {"delta": {"content": False}},
            ]
        },
    )
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert_malformed_chat_failed(result)


@pytest.mark.parametrize(
    "first,second",
    [
        ("length", "stop"),
        ("content_filter", "stop"),
        ("tool_calls", "stop"),
        ("function_call", "stop"),
        ("stop", "tool_calls"),
    ],
)
def test_conflicting_finish_metadata_cannot_replace_an_earlier_terminal_reason(first, second):
    initial = finished_chat_frame()
    initial["choices"][0]["finish_reason"] = first
    raw = sse(initial, {"choices": [{"delta": {}, "finish_reason": second}]})
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    assert_malformed_chat_failed(result)


@pytest.mark.parametrize(
    "finish", ["stop", "length", "content_filter", "tool_calls", "function_call"]
)
@pytest.mark.parametrize("later", [None, "repeat"])
def test_null_or_repeated_finish_metadata_preserves_the_terminal_reason(finish, later):
    initial = finished_chat_frame()
    initial["choices"][0]["finish_reason"] = finish
    raw = sse(initial, {"choices": [{"delta": {}, "finish_reason": finish if later else None}]})
    result = events(b"".join(proxy.translate_chat_stream(io.BytesIO(raw).read, model="m")))
    expected = "incomplete" if finish in ("length", "content_filter") else "completed"
    assert result[-1]["type"] == f"response.{expected}"


@pytest.mark.parametrize("kind", ["responses", "failover"])
def test_conflicting_finish_after_length_is_failed_on_runtime_routes(kind):
    initial = finished_chat_frame()
    initial["choices"][0]["finish_reason"] = "length"
    raw = sse(initial, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
    with Upstream(429, b"quota") as primary:
        with Upstream(body=raw, content_type="text/event-stream") as upstream:
            server = (
                proxy.ResponsesToChatProxy(upstream.url)
                if kind == "responses"
                else proxy.FailoverProxy(spec(primary.url, upstream.url))
            )
            with server:
                local_path = "/responses" if kind == "responses" else "/oc/p/responses"
                status, _, output = request(
                    server.base_url + local_path,
                    {"model": "m", "input": "hello"},
                    [(proxy.CAPABILITY_HEADER, server.capability)],
                )
    assert status == 200
    assert_malformed_chat_failed(events(output))


def test_equals_json_argv_is_redacted_before_encoding():
    token = 'FAKE-QUOTE"SLASH\\TAIL'
    argv = main._diagnostic_argv(
        [
            "claude",
            "--settings=" + json.dumps({"apiKey": token, "prompt": "Unicode 你好 1"}),
        ],
        [token, "1"],
    )
    assert json.loads(argv[1].split("=", 1)[1]) == {"apiKey": "***", "prompt": "Unicode 你好 1"}


def test_required_switch_is_not_a_prompt_or_numeric_secret():
    document = {"prompt": "1", "count": 1, "enabled": True, "apiKey": "1"}
    rendered = main._diagnostic_document(json.dumps(document), ["1"], ".json")
    assert json.loads(rendered) == {"prompt": "1", "count": 1, "enabled": True, "apiKey": "***"}


@pytest.mark.parametrize("token", ['FAKE-QUOTE"SLASH\\TAIL', "FAKE-秘密-é", "1"])
def test_actual_kimi_generated_config_printer_redacts_typed_key(token, capsys):
    launch = provider.build_launch(
        {
            "harness": "kimi",
            "secretEnv": "TOKEN",
            "config": {
                "baseUrl": "http://127.0.0.1:1",
                "model": "Unicode-你好-1",
            },
        },
        {"TOKEN": token},
    )
    original = launch.config_files
    main._print_config_files(launch)
    output = capsys.readouterr().out
    diagnostic = tomllib.loads(
        "\n".join(line[4:] for line in output.splitlines() if line.startswith("    "))
    )
    assert diagnostic["providers"]["agedum"]["api_key"] == "***"
    assert diagnostic["models"]["Unicode-你好-1"]["model"] == "Unicode-你好-1"
    assert launch.config_files == original


@pytest.mark.parametrize(
    "fallback_stream",
    [
        sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        sse({"choices": [{"delta": {"content": "partial"}}]}, done=False),
        sse({"error": {"message": "generation failed"}}),
    ],
)
def test_oauth_opencode_runtime_headers_survive_primary_and_translated_fallback(
    fallback_stream,
    monkeypatch,
    capsys,
):
    with Upstream(429, b"quota") as primary:
        with Upstream(body=fallback_stream, content_type="text/event-stream") as fallback:
            monkeypatch.setattr(provider, "OPENAI_CODEX_UPSTREAM", primary.url)
            config = {
                "harness": "opencode",
                "requiredEnv": ["TOKEN"],
                "config": {
                    "model": "openai/m",
                    "providerDef": {
                        "id": "f",
                        "baseUrl": fallback.url,
                        "apiKeyEnv": "TOKEN",
                        "npm": "@ai-sdk/openai-compatible",
                    },
                    "opencodeConfig": {
                        "provider": {"f": {"models": {"m": {}}}},
                        "agent": {"worker": {"model": "openai/m"}},
                    },
                },
                "failover": {
                    "detect": {"status": [429], "messages": ["quota"]},
                    "chains": {"openai/m": ["f/m"]},
                    "vision": {"openai/m": True, "f/m": True},
                },
            }
            with main._maybe_failover_proxy(config, {"TOKEN": "FAKE-FALLBACK"}) as plan:
                launch = provider.build_launch(config, {"TOKEN": "FAKE-FALLBACK"}, failover=plan)
                document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
                options = document["provider"]["openai"]["options"]
                # OpenCode resolves env references, then its OAuth fetch replaces only auth.
                headers = {
                    name: os.environ[value[5:-1]] for name, value in options["headers"].items()
                }
                capability = headers[proxy.CAPABILITY_HEADER]
                headers["authorization"] = "Bearer FAKE-OAUTH"
                headers["ChatGPT-Account-Id"] = "FAKE-ACCOUNT"
                assert "/v1/responses" not in options["baseURL"]
                assert "/chat/completions" not in options["baseURL"]
                assert capability not in repr(launch)
                main._print_environment(launch)
                status, _, output = request(
                    options["baseURL"] + "/responses",
                    {
                        "model": "m",
                        "input": "hello",
                        "stream": True,
                    },
                    list(headers.items()),
                )
                assert status == 200
            assert proxy.CAPABILITY_ENV not in os.environ
    primary_headers = dict(primary.requests[0][1])
    assert primary_headers["authorization"] == "Bearer FAKE-OAUTH"
    assert primary_headers["ChatGPT-Account-Id"] == "FAKE-ACCOUNT"
    fallback_headers = dict(fallback.requests[0][1])
    assert fallback_headers["Authorization"] == "Bearer FAKE-FALLBACK"
    assert "FAKE-OAUTH" not in repr(fallback.requests)
    assert "FAKE-ACCOUNT" not in repr(fallback.requests)
    assert capability not in repr(primary.requests + fallback.requests)
    expected = (
        "completed"
        if b'"stop"' in fallback_stream
        else "failed"
        if b'"error"' in fallback_stream
        else "incomplete"
    )
    assert events(output)[-1]["type"] == f"response.{expected}"
    captured = capsys.readouterr()
    assert capability not in captured.out + captured.err + output.decode()


def test_capabilities_are_per_launch_and_not_upstream_secrets():
    with proxy.FoldProxy("http://127.0.0.1:1") as first:
        with proxy.FoldProxy("http://127.0.0.1:1") as second:
            assert len(first.capability) >= 43
            assert first.capability != second.capability
            assert first.capability not in first.base_url
            assert (
                request(
                    first.base_url,
                    headers=[
                        (proxy.CAPABILITY_HEADER, second.capability),
                    ],
                )[0]
                == 401
            )


def test_runtime_environment_restores_after_client_exception(monkeypatch):
    monkeypatch.setenv(proxy.CAPABILITY_ENV, "original")
    monkeypatch.setenv("AGEDUM_CODEX_CHAT_UPSTREAM", "http://127.0.0.1:1")
    with pytest.raises(RuntimeError):
        with main._maybe_codex_proxy("codex", ["codex"]):
            assert os.environ[proxy.CAPABILITY_ENV] != "original"
            raise RuntimeError("child failed")
    assert os.environ[proxy.CAPABILITY_ENV] == "original"
