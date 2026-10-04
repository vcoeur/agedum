"""Regression versions of the credential/stream audit's localhost reproductions."""

import contextlib
import gzip
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
        target = (address.path or "/") + ("?" + address.query if "?" in url else "")
        connection.putrequest(method, target)
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


class StreamUpstream:
    """Flush a first SSE frame and optionally hold the terminal frame behind a gate."""

    def __init__(
        self,
        first,
        tail=b"",
        *,
        framing="close",
        pause=False,
        encoding=None,
        negotiate=False,
        truncate_chunked=False,
    ):
        self.requests = []
        self.flushed = threading.Event()
        self.release = threading.Event()
        self.ended = threading.Event()
        if not pause:
            self.release.set()
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                state.requests.append((self.path, list(self.headers.items()), raw))
                wire_first, wire_tail = first, tail
                wire_encoding = encoding
                if negotiate and "identity" in self.headers.get_all("Accept-Encoding", []):
                    wire_encoding = None
                if wire_encoding == "gzip" and negotiate:
                    wire_first, wire_tail = gzip.compress(first + tail), b""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if wire_encoding:
                    self.send_header("Content-Encoding", wire_encoding)
                if framing == "length":
                    self.send_header("Content-Length", str(len(wire_first + wire_tail)))
                elif framing == "chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()

                def write(payload):
                    if not payload:
                        return
                    if framing == "chunked":
                        self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                    else:
                        self.wfile.write(payload)
                    self.wfile.flush()

                try:
                    write(wire_first)
                    state.flushed.set()
                    if not state.release.wait(10):
                        return
                    write(wire_tail)
                    if framing == "chunked" and not truncate_chunked:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    state.ended.set()

            do_GET = do_POST

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
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextlib.contextmanager
def streaming_proxy(kind, upstream):
    if kind == "fold":
        with proxy.FoldProxy(upstream) as server:
            yield server, "/v1/messages", {"model": "m", "messages": []}
    elif kind == "responses":
        with proxy.ResponsesToChatProxy(upstream) as server:
            yield server, "/responses", {"model": "m", "input": "hello"}
    elif kind == "anthropic":
        with proxy.TranslateProxy(upstream, api_key="FAKE-PRIVATE") as server:
            yield server, "/v1/messages", {"model": "m", "messages": [], "stream": True}
    elif kind == "translated-failover":
        with Upstream(429, b"quota") as primary:
            config = spec(primary.url, upstream)
            config["routes"]["p"]["openai"] = True
            with proxy.FailoverProxy(config) as server:
                yield server, "/oc/p/responses", {"model": "m", "input": "hello"}
    else:
        with proxy.FailoverProxy(spec(upstream, upstream)) as server:
            yield server, "/oc/p/chat/completions", {"model": "m", "messages": []}


@pytest.mark.parametrize(
    "kind", ["fold", "failover", "responses", "translated-failover", "anthropic"]
)
@pytest.mark.parametrize("framing", ["close", "length", "chunked"])
def test_flushed_payload_arrives_before_upstream_eof(kind, framing):
    first = sse({"choices": [{"delta": {"content": "early-text"}}]}, done=False)
    tail = sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    with StreamUpstream(first, tail, framing=framing, pause=True) as upstream:
        with streaming_proxy(kind, upstream.url) as (server, local_path, body):
            address = urlsplit(server.base_url)
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
            try:
                connection.request(
                    "POST",
                    local_path,
                    json.dumps(body),
                    headers={
                        proxy.CAPABILITY_HEADER: server.capability,
                        "Authorization": "Bearer FAKE-PRIMARY",
                    },
                )
                response = connection.getresponse()
                assert response.status == 200
                assert upstream.flushed.wait(2)
                # response.created is synthetic; require the actual upstream text delta.
                while True:
                    line = response.readline()
                    assert line, "stream ended before delivering its first payload"
                    if b"early-text" in line:
                        break
                assert not upstream.release.is_set()
                assert not upstream.ended.is_set()
                upstream.release.set()
                remainder = response.read()
                if kind in ("responses", "translated-failover"):
                    assert events(remainder)[-1]["type"] == "response.completed"
                elif kind == "anthropic":
                    assert events(remainder)[-1]["type"] == "message_stop"
            finally:
                upstream.release.set()
                connection.close()


@pytest.mark.parametrize("kind", ["responses", "translated-failover"])
def test_responses_negotiate_identity_with_gzip_capable_upstream(kind):
    raw = sse({"choices": [{"delta": {"content": "not lost"}, "finish_reason": "stop"}]})
    with StreamUpstream(raw, encoding="gzip", negotiate=True) as upstream:
        with streaming_proxy(kind, upstream.url) as (server, local_path, body):
            status, headers, output = request(
                server.base_url + local_path,
                body,
                [
                    (proxy.CAPABILITY_HEADER, server.capability),
                    ("accept-encoding", "gzip"),
                    ("aCcEpT-EnCoDiNg", "br, gzip"),
                ],
            )
    assert status == 200
    encodings = [
        value for name, value in upstream.requests[0][1] if name.lower() == "accept-encoding"
    ]
    assert encodings == ["identity"]
    terminal = events(output)[-1]
    assert terminal["type"] == "response.completed"
    assert terminal["response"]["output"][0]["content"][0]["text"] == "not lost"
    assert not any(name.lower() == "content-encoding" for name, _ in headers)


@pytest.mark.parametrize("kind", ["responses", "translated-failover"])
@pytest.mark.parametrize(
    "encoding,payload",
    [
        ("gzip", gzip.compress(sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))),
        ("gzip", b"not a gzip stream"),
        ("br", b"unsupported"),
    ],
)
def test_responses_reject_unsolicited_encoding_without_false_success(kind, encoding, payload):
    with StreamUpstream(payload, encoding=encoding) as upstream:
        with streaming_proxy(kind, upstream.url) as (server, local_path, body):
            status, _, raw = request(
                server.base_url + local_path,
                body,
                [
                    (proxy.CAPABILITY_HEADER, server.capability),
                ],
            )
    assert status == 502
    assert (
        json.loads(raw)["error"]["message"] == "agedum proxy: unsupported upstream Content-Encoding"
    )
    assert b"response.completed" not in raw


@pytest.mark.parametrize(
    "method,body",
    [("GET", None), ("POST", {"model": "unmapped"}), ("POST", {"model": "m", "messages": []})],
)
@pytest.mark.parametrize("suffix", ["?version=2&tag=a&tag=b%2Fc&next=%3F%26", "?"])
def test_failover_preserves_transparent_query(method, body, suffix):
    with (
        Upstream() as upstream,
        proxy.FailoverProxy(spec(upstream.url + "/v1", upstream.url)) as server,
    ):
        status, _, _ = request(
            server.base_url + "/oc/p/custom" + suffix,
            body,
            [
                (proxy.CAPABILITY_HEADER, server.capability),
            ],
            method=method,
        )
    assert status == 200
    assert upstream.requests[0][0] == "/v1/custom" + suffix


@pytest.mark.parametrize("translated", [False, True])
def test_fallback_preserves_queries_except_deliberate_responses_translation(translated):
    terminal = sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    with (
        Upstream(429, b"quota") as primary,
        Upstream(200, terminal, "text/event-stream") as fallback,
    ):
        config = spec(primary.url + "/v1", fallback.url + "/v2")
        config["routes"]["p"]["openai"] = translated
        body = {"model": "m", "input": "hello"} if translated else {"model": "m", "messages": []}
        with proxy.FailoverProxy(config) as server:
            status, _, _ = request(
                server.base_url + "/oc/p/custom?version=2",
                body,
                [
                    (proxy.CAPABILITY_HEADER, server.capability),
                ],
            )
    assert status == 200
    assert primary.requests[0][0] == "/v1/custom?version=2"
    assert fallback.requests[0][0] == (
        "/v2/chat/completions" if translated else "/v2/custom?version=2"
    )


def interleaved_tool_chunks():
    # Genuine 0,1,0,1 interleaving; arguments precede one call's identity and repeat
    # the other call's identity without duplicating its name or dropping late arguments.
    calls = [
        {"index": 0, "function": {"arguments": '{"city":'}},
        {"index": 1, "id": "call_b", "function": {"name": "lookup", "arguments": '{"n":'}},
        {"index": 0, "id": "call_a", "function": {"name": "weather", "arguments": '"Paris"}'}},
        {"index": 1, "id": "call_b", "function": {"name": "lookup", "arguments": "2}"}},
    ]
    return [
        *[{"choices": [{"delta": {"tool_calls": [call]}}]} for call in calls],
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


def assert_tool_lifecycles(output):
    open_index = None
    blocks = []
    fragments = []
    for event in output:
        if event["type"] == "content_block_start":
            assert open_index is None
            open_index = event["index"]
            blocks.append(event["content_block"])
            fragments = []
        elif event["type"] == "content_block_delta":
            assert event["index"] == open_index
            fragments.append(event["delta"]["partial_json"])
        elif event["type"] == "content_block_stop":
            assert event["index"] == open_index
            blocks[-1]["input"] = json.loads("".join(fragments))
            open_index = None
        elif event["type"] in ("message_delta", "message_stop"):
            assert open_index is None
    assert open_index is None
    assert blocks == [
        {"type": "tool_use", "id": "call_a", "name": "weather", "input": {"city": "Paris"}},
        {"type": "tool_use", "id": "call_b", "name": "lookup", "input": {"n": 2}},
    ]


def test_anthropic_interleaved_tools_are_buffered_then_serialized():
    stream = proxy.OpenAIToAnthropicStream("m")
    chunks = interleaved_tool_chunks()
    early = b"".join(frame for chunk in chunks for frame in stream.feed(chunk))
    assert all(event["type"] == "message_start" for event in events(early))
    proxy._feed_sse_line(stream, b"data: [DONE]\n")
    assert_tool_lifecycles(events(early + b"".join(stream.finish())))
    assert stream.finish() == []


def test_anthropic_interleaved_tools_over_real_localhost_stream():
    with StreamUpstream(sse(*interleaved_tool_chunks())) as upstream:
        with proxy.TranslateProxy(upstream.url, api_key="FAKE-PRIVATE") as server:
            status, _, raw = request(
                server.base_url + "/v1/messages",
                {"model": "m", "messages": [], "stream": True},
                [
                    (proxy.CAPABILITY_HEADER, server.capability),
                ],
            )
    assert status == 200
    assert_tool_lifecycles(events(raw))


def anthropic_tool_chunk(arguments='{"x":1}', *, tool_id="call_a", name="lookup"):
    return {
        "choices": [
            {
                "delta": {
                    "content": "partial text",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": tool_id,
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            }
        ],
    }


def anthropic_tool_wire(arguments='{"x":1}', *, finish="tool_calls", done=True):
    chunks = [anthropic_tool_chunk(arguments)]
    if finish is not None:
        chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    return sse(*chunks, done=done)


_ANTHROPIC_TOOL_CASES = [
    pytest.param(
        anthropic_tool_wire('{"x":', finish=None, done=False), "upstream_eof", id="partial-eof"
    ),
    pytest.param(
        anthropic_tool_wire(finish=None, done=False), "upstream_eof", id="complete-arguments-eof"
    ),
    pytest.param(
        anthropic_tool_wire('{"x":', done=False), "upstream_eof", id="partial-finish-without-done"
    ),
    pytest.param(anthropic_tool_wire(done=False), "upstream_eof", id="finish-without-done"),
    pytest.param(anthropic_tool_wire(finish=None), "upstream_eof", id="done-without-finish"),
    pytest.param(
        sse(anthropic_tool_chunk('{"x":'), {"error": {"message": "FAKE-SECRET upstream failed"}}),
        "upstream_error",
        id="error-partial-arguments",
    ),
    pytest.param(
        anthropic_tool_wire(done=False) + sse({"error": {"message": "FAKE-SECRET"}}),
        "upstream_error",
        id="error-after-successful-finish",
    ),
    pytest.param(
        anthropic_tool_wire('{"x":', finish="length"), "max_output_tokens", id="length-partial"
    ),
    pytest.param(
        anthropic_tool_wire(finish="length"), "max_output_tokens", id="length-complete-arguments"
    ),
    pytest.param(
        anthropic_tool_wire(finish="content_filter"),
        "unsupported_finish_reason",
        id="content-filter",
    ),
    pytest.param(anthropic_tool_wire('{"x":'), "invalid_tool_arguments", id="unfinished-json"),
    pytest.param(anthropic_tool_wire("not-json"), "invalid_tool_arguments", id="malformed-json"),
    *[
        pytest.param(anthropic_tool_wire(value), "invalid_tool_arguments", id="non-object-" + value)
        for value in ("[]", "null", "1", '"string"')
    ],
    pytest.param(
        anthropic_tool_wire('{"x":NaN}'), "invalid_tool_arguments", id="non-json-constant"
    ),
    pytest.param(
        sse(
            anthropic_tool_chunk(tool_id=None),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
        "invalid_tool_arguments",
        id="missing-id",
    ),
    pytest.param(
        sse(
            anthropic_tool_chunk(name=""),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
        "invalid_tool_arguments",
        id="missing-name",
    ),
    pytest.param(
        anthropic_tool_wire(done=False) + b"data: not-json\n\ndata: [DONE]\n\n",
        "invalid_upstream_stream",
        id="malformed-after-finish",
    ),
    pytest.param(
        anthropic_tool_wire(done=False)
        + sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "invalid_upstream_stream",
        id="conflicting-finish",
    ),
    pytest.param(
        anthropic_tool_wire(done=False) + sse({"choices": [{"delta": {"tool_calls": False}}]}),
        "invalid_upstream_stream",
        id="invalid-tool-container-after-finish",
    ),
    pytest.param(
        anthropic_tool_wire(done=False) + b'data: {"choices":',
        "invalid_upstream_stream",
        id="truncated-sse-json",
    ),
    pytest.param(
        anthropic_tool_wire(done=False) + b'data: {"id":"\xff"}\n\n',
        "invalid_upstream_stream",
        id="invalid-utf8",
    ),
    pytest.param(
        sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "invalid_tool_arguments",
        id="tool-finish-without-tools",
    ),
    pytest.param(
        anthropic_tool_wire(finish="unknown"), "invalid_upstream_stream", id="unknown-finish"
    ),
    pytest.param(
        anthropic_tool_wire(finish="function_call"), "unsupported_finish_reason", id="legacy-finish"
    ),
    pytest.param(
        anthropic_tool_wire(done=False) + sse({"choices": [{"delta": {"refusal": "refused"}}]}),
        "upstream_refusal",
        id="refusal-after-finish",
    ),
    pytest.param(
        anthropic_tool_wire(done=False)
        + sse({"choices": [{"delta": {"function_call": {"name": "legacy"}}}]}),
        "unsupported_function_call",
        id="legacy-output-after-finish",
    ),
    pytest.param(
        anthropic_tool_wire(done=False)
        + sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_b",
                                    "function": {"name": "second", "arguments": '{"y":'},
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        "invalid_tool_arguments",
        id="one-invalid-call-withholds-all-tools",
    ),
    pytest.param(anthropic_tool_wire(), None, id="intact-tool-finish"),
    pytest.param(anthropic_tool_wire(finish="stop"), None, id="intact-stop-finish"),
    pytest.param(
        anthropic_tool_wire(done=False)
        + sse(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [{"delta": {}, "finish_reason": None}]},
            {"choices": [], "usage": None},
        ),
        None,
        id="repeated-finish-null-metadata",
    ),
]


def assert_anthropic_terminal(output, failure):
    if failure:
        assert output[-1] == {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": "agedum translate-proxy: " + failure,
            },
        }
        assert not any(event["type"] in ("message_delta", "message_stop") for event in output)
        # No buffered tool becomes visible, so even eager clients cannot execute it.
        assert not any(event.get("content_block", {}).get("type") == "tool_use" for event in output)
        assert not any(event.get("delta", {}).get("type") == "input_json_delta" for event in output)
        open_blocks = {
            event["index"]: event["content_block"]["type"]
            for event in output
            if event["type"] == "content_block_start"
        }
        assert all(
            open_blocks[event["index"]] == "text"
            for event in output
            if event["type"] == "content_block_stop"
        )
        assert "FAKE-SECRET" not in json.dumps(output)
    else:
        assert output[-1]["type"] == "message_stop"
        assert output[-2]["delta"]["stop_reason"] == "tool_use"
        assert not any(event["type"] == "error" for event in output)
        fragments = [
            event["delta"]["partial_json"]
            for event in output
            if event.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert json.loads("".join(fragments)) == {"x": 1}


@pytest.mark.parametrize("raw,failure", _ANTHROPIC_TOOL_CASES)
def test_anthropic_buffered_tools_terminal_matrix(raw, failure):
    stream = proxy.OpenAIToAnthropicStream("m")
    output = []
    for line in raw.splitlines(keepends=True):
        output.extend(proxy._feed_sse_line(stream, line))
    output.extend(stream.finish())
    assert_anthropic_terminal(events(b"".join(output)), failure)
    assert stream.finish() == []
    assert stream.feed(anthropic_tool_chunk()) == []


@pytest.mark.parametrize("raw,failure", _ANTHROPIC_TOOL_CASES)
def test_anthropic_buffered_tools_terminal_matrix_over_localhost(raw, failure):
    with StreamUpstream(raw) as upstream:
        with proxy.TranslateProxy(upstream.url, api_key="FAKE-PRIVATE") as server:
            status, headers, output = request(
                server.base_url + "/v1/messages?beta=true",
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": "lookup"}],
                    "stream": True,
                },
                [(proxy.CAPABILITY_HEADER, server.capability)],
            )
    assert status == 200
    assert ("Content-Type", "text/event-stream") in headers
    assert_anthropic_terminal(events(output), failure)
    if failure:
        assert b"event: error\n" in output
    assert upstream.requests[0][0] == "/v1/chat/completions"
    assert json.loads(upstream.requests[0][2])["stream"] is True
    assert not any(
        name.lower() == proxy.CAPABILITY_HEADER.lower() for name, _ in upstream.requests[0][1]
    )


def test_anthropic_exact_review_error_and_eof_do_not_stop_partial_tools():
    chunk = anthropic_tool_chunk('{"x":')
    chunk["choices"][0]["delta"].pop("content")
    for error in (False, True):
        stream = proxy.OpenAIToAnthropicStream("m")
        output = stream.feed(chunk)
        if error:
            output += proxy._feed_sse_line(
                stream, b'data: {"error":{"message":"upstream failed"}}\n'
            )
        output += stream.finish()
        assert stream.tool_blocks[0]["arguments"] == ['{"x":']
        assert_anthropic_terminal(
            events(b"".join(output)), "upstream_error" if error else "upstream_eof"
        )
        assert not any(event["type"] == "content_block_stop" for event in events(b"".join(output)))


def test_anthropic_chunked_disconnect_does_not_finalize_buffered_tools():
    raw = sse(anthropic_tool_chunk('{"x":'), done=False)
    with StreamUpstream(raw, framing="chunked", truncate_chunked=True) as upstream:
        with proxy.TranslateProxy(upstream.url, api_key="FAKE-PRIVATE") as server:
            status, _, output = request(
                server.base_url + "/v1/messages",
                {
                    "model": "m",
                    "messages": [],
                    "stream": True,
                },
                [(proxy.CAPABILITY_HEADER, server.capability)],
            )
    assert status == 200
    assert_anthropic_terminal(events(output), "upstream_disconnect")


@pytest.mark.parametrize(
    "finish,done,failure,stop",
    [
        ("stop", True, None, "end_turn"),
        ("length", True, None, "max_tokens"),
        ("length", False, "upstream_eof", None),
        (None, False, "upstream_eof", None),
        ("content_filter", True, "unsupported_finish_reason", None),
    ],
)
def test_anthropic_text_only_truncation_contract_over_localhost(finish, done, failure, stop):
    raw = sse(
        {"choices": [{"delta": {"content": "partial text"}, "finish_reason": finish}]}, done=done
    )
    with StreamUpstream(raw) as upstream, proxy.TranslateProxy(upstream.url) as server:
        status, _, output = request(
            server.base_url + "/v1/messages",
            {
                "model": "m",
                "messages": [],
                "stream": True,
            },
            [(proxy.CAPABILITY_HEADER, server.capability)],
        )
    assert status == 200
    parsed = events(output)
    assert [
        event["delta"]["text"]
        for event in parsed
        if event.get("delta", {}).get("type") == "text_delta"
    ] == ["partial text"]
    if failure:
        assert_anthropic_terminal(parsed, failure)
    else:
        assert parsed[-1]["type"] == "message_stop"
        assert parsed[-2]["delta"]["stop_reason"] == stop


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


def test_exact_short_secret_is_masked_without_changing_numeric_types():
    document = {"prompt": "1", "count": 1, "enabled": True, "apiKey": "1"}
    rendered = main._diagnostic_document(json.dumps(document), ["1"], ".json")
    assert json.loads(rendered) == {"prompt": "***", "count": 1, "enabled": True, "apiKey": "***"}


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
