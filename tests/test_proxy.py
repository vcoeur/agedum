import contextlib
import http.client
import json
import socket
import struct
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from agedum.proxy import (
    FoldProxy,
    OpenAIToAnthropicStream,
    ResponsesToChatProxy,
    TranslateProxy,
    _FoldHandler,
    _QuietThreadingHTTPServer,
    anthropic_to_openai_request,
    fold_system_messages,
    openai_to_anthropic_response,
    responses_to_chat_request,
    translate_chat_stream,
)

# ---------------------------------------------------------------------------
# fold_system_messages — pure transform
# ---------------------------------------------------------------------------


def test_fold_string_system_message_into_array_top_level_system():
    # The captured real-world shape: messages=[user, system], system already an array.
    body = {
        "system": [{"type": "text", "text": "base prompt"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "system", "content": "hook context"},
        ],
    }
    folded = fold_system_messages(body)
    assert [m["role"] for m in folded["messages"]] == ["user"]
    assert folded["system"] == [
        {"type": "text", "text": "base prompt"},
        {"type": "text", "text": "hook context"},
    ]
    # original untouched
    assert [m["role"] for m in body["messages"]] == ["user", "system"]


def test_fold_promotes_string_system_to_array():
    body = {
        "system": "base prompt",
        "messages": [{"role": "system", "content": "extra"}],
    }
    folded = fold_system_messages(body)
    assert folded["messages"] == []
    assert folded["system"] == [
        {"type": "text", "text": "base prompt"},
        {"type": "text", "text": "extra"},
    ]


def test_fold_with_no_top_level_system():
    body = {"messages": [{"role": "system", "content": "only"}]}
    folded = fold_system_messages(body)
    assert folded["system"] == [{"type": "text", "text": "only"}]
    assert folded["messages"] == []


def test_fold_extracts_text_from_block_list_content():
    body = {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            },
        ]
    }
    folded = fold_system_messages(body)
    assert folded["system"] == [{"type": "text", "text": "a\nb"}]


def test_no_system_message_is_identity():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert fold_system_messages(body) is body


def test_no_messages_list_is_identity():
    body = {"system": "x"}
    assert fold_system_messages(body) is body


def test_multiple_system_messages_preserve_order():
    body = {
        "messages": [
            {"role": "system", "content": "one"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "two"},
        ]
    }
    folded = fold_system_messages(body)
    assert [m["role"] for m in folded["messages"]] == ["user"]
    assert folded["system"] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]


# ---------------------------------------------------------------------------
# FoldProxy — live end-to-end against a fake upstream
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """Records the last request body and replies with a fixed payload."""

    def __init__(self):
        self.last_body = None
        self.last_path = None
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            state.last_body = json.loads(self.rfile.read(length))
            state.last_path = self.path
            # Reject any system role, mimicking DeepSeek's strict serde endpoint.
            roles = [m.get("role") for m in state.last_body.get("messages", [])]
            if "system" in roles:
                payload = json.dumps({"error": "unknown variant `system`"}).encode()
                self.send_response(400)
            else:
                payload = json.dumps({"ok": True}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

        do_PUT = do_POST

    return Handler


def test_proxy_folds_before_forwarding():
    request_body = {
        "system": [{"type": "text", "text": "base"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "system", "content": "hook context"},
        ],
    }
    with _FakeUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps(request_body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"ok": True}

    # Upstream saw the folded shape: no system role, hook text in top-level system.
    assert [m["role"] for m in upstream.last_body["messages"]] == ["user"]
    assert upstream.last_body["system"][-1] == {"type": "text", "text": "hook context"}
    assert upstream.last_path == "/v1/messages"


def test_proxy_passes_non_message_bodies_through():
    with _FakeUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
    assert [m["role"] for m in upstream.last_body["messages"]] == ["user"]


def test_proxy_reads_chunked_request_body():
    # http.server does not de-chunk request bodies itself; the proxy must, or a chunked
    # request would be forwarded body-less. The de-chunked body is folded and re-framed
    # with Content-Length for the upstream hop.
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "hook context"},
        ]
    }
    payload = json.dumps(body).encode()
    with _FakeUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        target = urlsplit(proxy.base_url)
        connection = http.client.HTTPConnection(target.hostname, target.port)
        try:
            connection.request(
                "POST",
                "/v1/messages",
                body=iter([payload[:7], payload[7:]]),  # no len() -> chunked encoding
                headers={"Content-Type": "application/json"},
                encode_chunked=True,
            )
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"ok": True}
        finally:
            connection.close()
    # Upstream saw the complete, de-chunked, folded body.
    assert [m["role"] for m in upstream.last_body["messages"]] == ["user"]
    assert upstream.last_body["system"] == [{"type": "text", "text": "hook context"}]


def test_proxy_forwards_put_requests():
    # The Anthropic-compat surface is POST/GET/DELETE today, but the proxy is a generic
    # forwarder — other verbs must pass through rather than 501 at the proxy itself.
    with _FakeUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/v1/thing",
            data=json.dumps({"messages": []}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
    assert upstream.last_body == {"messages": []}


class _DisconnectingUpstream:
    """Accepts the connection, drains the request, then closes without replying.

    Reproduces ``http.client.RemoteDisconnected`` inside the proxy's ``urlopen``
    (raised from ``getresponse()``) — the exact failure that previously crashed the
    handler thread with an unhandled traceback.
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def base_url(self):
        host, port = self._sock.getsockname()[:2]
        return f"http://{host}:{port}"

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # listening socket closed on __exit__
            conn.recv(65536)  # drain the forwarded request, then drop it on the floor
            conn.close()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._sock.close()


def test_proxy_returns_502_when_upstream_disconnects():
    # A dropped upstream socket must surface as a clean 502, not a crashed handler thread.
    with _DisconnectingUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            assert exc.code == 502
            assert json.loads(exc.read())["error"]["type"] == "api_error"
        else:
            raise AssertionError("expected a 502 from the proxy")


# ---------------------------------------------------------------------------
# _QuietThreadingHTTPServer — a peer hanging up is routine, not an error
# ---------------------------------------------------------------------------


def test_quiet_server_swallows_connection_teardown(capsys):
    # Every connection-level family a proxy routinely sees — a reset before the request
    # line, a BrokenPipe mid-stream, an aborted upload — must be absorbed silently at the
    # one seam, wherever in the handler it was raised.
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _FoldHandler)
    try:
        for error in (
            ConnectionResetError(),
            BrokenPipeError(),
            ConnectionAbortedError(),
            TimeoutError(),
        ):
            try:
                raise error
            except (ConnectionError, TimeoutError):
                server.handle_error(None, ("127.0.0.1", 12345))
    finally:
        server.server_close()
    assert capsys.readouterr().err == ""


def test_quiet_server_still_reports_real_errors(capsys):
    # The disconnect seam must not hide a genuine bug.
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _FoldHandler)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 12345))
    finally:
        server.server_close()
    assert "ValueError" in capsys.readouterr().err


def test_proxy_survives_client_reset_before_request():
    # A client that opens a socket and resets it before sending a request line (the idle
    # keep-alive socket being reaped) must not take the proxy down — it keeps serving.
    with _FakeUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        target = urlsplit(proxy.base_url)
        resetting = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        resetting.connect((target.hostname, target.port))
        # SO_LINGER with a zero timeout makes close() send a RST, not a graceful FIN.
        resetting.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        resetting.close()

        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200


def test_proxy_relays_close_delimited_not_chunked():
    # Tier 2: the response is delimited by connection close — no chunked re-framing, no
    # Content-Length — so the client keeps no idle socket to reset afterwards.
    with _FakeUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.headers.get("Connection", "").lower() == "close"
            assert response.headers.get("Transfer-Encoding") is None
            assert json.loads(response.read()) == {"ok": True}


# ---------------------------------------------------------------------------
# Mid-stream upstream drop — the relay-phase guard (kept from the prior fix)
# ---------------------------------------------------------------------------


class _MidBodyDropUpstream:
    """Sends a valid 200 with a large Content-Length, a few body bytes, then closes.

    The status line and headers arrive intact, so the proxy's forward succeeds and
    ``_relay`` begins streaming them downstream — but the body ends far short of the
    promised length, so ``response.read()`` *inside* ``_relay`` raises
    ``http.client.IncompleteRead``. This is the mid-stream upstream drop that the
    connect-phase 502 guard cannot catch (the response is already in flight).
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def base_url(self):
        host, port = self._sock.getsockname()[:2]
        return f"http://{host}:{port}"

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # listening socket closed on __exit__
            conn.recv(65536)  # drain the forwarded request
            # Promise far more than we deliver, then drop -> IncompleteRead on the next read.
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 1048576\r\n"
                b"\r\n"
                b'{"partial":'
            )
            conn.close()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._sock.close()


def test_proxy_survives_upstream_drop_mid_stream():
    # An upstream that dies mid-body — after status + headers are already relayed downstream —
    # must not crash the handler thread. No 502 is possible (the response is in flight), so
    # _relay has to swallow the disconnect and stop quietly. We detect a crashed handler
    # thread by hooking the server's handle_error, which socketserver invokes only when an
    # exception escapes the request handler.
    handler_errors = []
    with _MidBodyDropUpstream() as upstream, FoldProxy(upstream.base_url) as proxy:
        # Make handler threads non-daemon so the proxy's server_close() (on __exit__) joins
        # the in-flight handler before we assert — otherwise the assertion races ahead of the
        # handler thread and a crash (handle_error) can land after the check.
        proxy._server.daemon_threads = False
        proxy._server.handle_error = lambda request, client_address: handler_errors.append(
            client_address
        )
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # The client sees a truncated stream — the honest outcome of a mid-flight upstream
        # drop. What must NOT happen is the handler thread dying with an unhandled traceback.
        with contextlib.suppress(http.client.HTTPException, urllib.error.URLError, OSError):
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
    assert handler_errors == []


# ---------------------------------------------------------------------------
# anthropic_to_openai_request — pure request transform
# ---------------------------------------------------------------------------


def test_request_folds_system_to_leading_message():
    out = anthropic_to_openai_request(
        {
            "system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out["messages"][0] == {"role": "system", "content": "a\n\nb"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_string_system_and_model_override():
    out = anthropic_to_openai_request(
        {"system": "be brief", "model": "claude-sonnet", "messages": []}, model="kimi-k2.7-code"
    )
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["model"] == "kimi-k2.7-code"  # config model wins over the body's


def test_request_tool_use_and_tool_result_round_trip():
    out = anthropic_to_openai_request(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "let me check"},
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "get_weather",
                            "input": {"city": "Paris"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "sunny"}
                    ],
                },
            ]
        }
    )
    assistant = out["messages"][0]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "let me check"
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"
    # object input is serialised to a JSON arguments string
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"city": "Paris"}
    tool = out["messages"][1]
    assert tool == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}


def test_request_tool_result_error_and_block_content():
    out = anthropic_to_openai_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "x",
                            "is_error": True,
                            "content": [{"type": "text", "text": "boom"}],
                        }
                    ],
                }
            ]
        }
    )
    assert out["messages"][0]["content"] == "[tool error] boom"


def test_request_image_block_to_data_url():
    out = anthropic_to_openai_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "AAAA",
                            },
                        },
                    ],
                }
            ]
        }
    )
    parts = out["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_request_strips_rejected_schema_formats():
    out = anthropic_to_openai_request(
        {
            "messages": [],
            "tools": [
                {
                    "name": "fetch",
                    "description": "get a url",
                    "input_schema": {
                        "type": "object",
                        "properties": {"url": {"type": "string", "format": "uri"}},
                    },
                }
            ],
        }
    )
    function = out["tools"][0]["function"]
    assert function["name"] == "fetch"
    assert "format" not in function["parameters"]["properties"]["url"]


def test_request_tool_choice_and_effort_and_stream_options():
    out = anthropic_to_openai_request(
        {
            "messages": [],
            "tools": [{"name": "t", "input_schema": {}}],
            "tool_choice": {"type": "any"},
            "stream": True,
        },
        reasoning_effort="high",
    )
    assert out["tool_choice"] == "required"  # any -> required
    assert out["reasoning_effort"] == "high"
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}


def test_request_tool_choice_specific_tool():
    out = anthropic_to_openai_request(
        {
            "messages": [],
            "tools": [{"name": "t", "input_schema": {}}],
            "tool_choice": {"type": "tool", "name": "t"},
        }
    )
    assert out["tool_choice"] == {"type": "function", "function": {"name": "t"}}


def test_request_injects_prompt_cache_key_when_set():
    out = anthropic_to_openai_request({"messages": []}, prompt_cache_key="sess-abc")
    assert out["prompt_cache_key"] == "sess-abc"


def test_request_omits_prompt_cache_key_when_unset():
    out = anthropic_to_openai_request({"messages": []})
    assert "prompt_cache_key" not in out


def test_request_thinking_toggle_maps_enabled_and_disabled():
    enabled = anthropic_to_openai_request(
        {"messages": [], "thinking": {"type": "enabled", "budget_tokens": 4096}},
        thinking_mode="toggle",
    )
    assert enabled["thinking"] == {"type": "enabled"}  # budget_tokens has no equivalent, dropped
    disabled = anthropic_to_openai_request(
        {"messages": [], "thinking": {"type": "disabled"}}, thinking_mode="toggle"
    )
    assert disabled["thinking"] == {"type": "disabled"}


def test_request_thinking_dropped_without_toggle_mode():
    # Default (no thinking_mode) keeps the historical drop — an always-think model must never
    # receive a `disabled`, so its provider leaves the mode unset.
    out = anthropic_to_openai_request({"messages": [], "thinking": {"type": "enabled"}})
    assert "thinking" not in out


def test_request_thinking_toggle_ignores_absent_or_unknown_type():
    absent = anthropic_to_openai_request({"messages": []}, thinking_mode="toggle")
    assert "thinking" not in absent
    unknown = anthropic_to_openai_request(
        {"messages": [], "thinking": {"type": "auto"}}, thinking_mode="toggle"
    )
    assert "thinking" not in unknown


def test_response_reads_flat_cached_tokens_fallback():
    # Standard OpenAI nests cached_tokens under prompt_tokens_details; a backend that reports it
    # flat on usage (e.g. Moonshot) is still surfaced as Anthropic cache_read_input_tokens.
    out = openai_to_anthropic_response(
        {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 6},
        }
    )
    assert out["usage"]["cache_read_input_tokens"] == 6


# ---------------------------------------------------------------------------
# openai_to_anthropic_response — pure non-streaming response transform
# ---------------------------------------------------------------------------


def test_response_tolerates_null_choice():
    # A backend emitting `choices: [null]` must not crash the transform with AttributeError.
    out = openai_to_anthropic_response({"choices": [None]})
    assert out["content"] == []
    assert out["stop_reason"] == "end_turn"


def test_response_text_and_usage():
    out = openai_to_anthropic_response(
        {
            "id": "chatcmpl-1",
            "model": "kimi",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        }
    )
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 4,
    }


def test_response_tool_calls_to_tool_use():
    out = openai_to_anthropic_response(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Paris"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert out["stop_reason"] == "tool_use"
    assert out["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}}
    ]


# ---------------------------------------------------------------------------
# OpenAIToAnthropicStream — OpenAI SSE -> Anthropic SSE state machine
# ---------------------------------------------------------------------------


def _drive(chunks):
    """Feed OpenAI chunk dicts through the stream and return parsed Anthropic events."""
    stream = OpenAIToAnthropicStream(model="kimi")
    raw: list[bytes] = []
    for chunk in chunks:
        raw += stream.feed(chunk)
    raw += stream.finish()
    return _parse_events(raw)


def _parse_events(byte_events):
    parsed = []
    for blob in byte_events:
        lines = blob.decode().strip().split("\n")
        event_type = lines[0].split("event:", 1)[1].strip()
        data = json.loads(lines[1].split("data:", 1)[1].strip())
        parsed.append((event_type, data))
    return parsed


def test_stream_text_only():
    events = _drive(
        [
            {
                "id": "c1",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}],
            },
            {"id": "c1", "model": "kimi", "choices": [{"index": 0, "delta": {"content": "Hello"}}]},
            {
                "id": "c1",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {"content": " world"}}],
            },
            {
                "id": "c1",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "c1",
                "model": "kimi",
                "choices": [],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        ]
    )
    types = [t for t, _ in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # message_start carries the upstream id/model
    assert events[0][1]["message"]["id"] == "c1"
    assert events[1][1] == {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    assert events[2][1]["delta"] == {"type": "text_delta", "text": "Hello"}
    delta = events[5][1]
    assert delta["delta"]["stop_reason"] == "end_turn"
    assert delta["usage"] == {"output_tokens": 2, "input_tokens": 7}


def test_stream_single_tool_call_streams_fragments():
    events = _drive(
        [
            {"id": "c", "model": "kimi", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
    )
    types = [t for t, _ in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = events[1][1]
    assert start["index"] == 0
    assert start["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {},
    }
    # arguments arrive as streamed fragments, not one buffered dump
    assert events[2][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"city":'}
    assert events[3][1]["delta"] == {"type": "input_json_delta", "partial_json": '"Paris"}'}
    assert events[5][1]["delta"]["stop_reason"] == "tool_use"


def test_stream_text_then_tool_closes_text_first():
    events = _drive(
        [
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {"content": "checking"}}],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "t", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
    )
    sequence = [(t, d.get("index")) for t, d in events]
    assert sequence == [
        ("message_start", None),
        ("content_block_start", 0),  # text block at index 0
        ("content_block_delta", 0),
        ("content_block_stop", 0),  # text closed before the tool opens
        ("content_block_start", 1),  # tool block at index 1
        ("content_block_delta", 1),
        ("content_block_stop", 1),
        ("message_delta", None),
        ("message_stop", None),
    ]


def test_stream_parallel_tool_calls_distinct_indices():
    events = _drive(
        [
            {"id": "c", "model": "kimi", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "a",
                                    "type": "function",
                                    "function": {"name": "foo", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "b",
                                    "type": "function",
                                    "function": {"name": "bar", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
    )
    starts = [d for t, d in events if t == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1]
    assert [s["content_block"]["name"] for s in starts] == ["foo", "bar"]
    stops = sorted(d["index"] for t, d in events if t == "content_block_stop")
    assert stops == [0, 1]
    # Anthropic requires one open block at a time: each start is followed by its stop
    # before the next start — i.e. the block-lifecycle events strictly alternate.
    lifecycle = [
        (t, d["index"]) for t, d in events if t in ("content_block_start", "content_block_stop")
    ]
    assert lifecycle == [
        ("content_block_start", 0),
        ("content_block_stop", 0),
        ("content_block_start", 1),
        ("content_block_stop", 1),
    ]


def test_stream_text_resumes_in_new_block_after_tool():
    # text -> tool -> text: the resumed text must open a fresh block, never a delta against
    # the already-stopped first text block.
    events = _drive(
        [
            {"id": "c", "model": "kimi", "choices": [{"index": 0, "delta": {"content": "before"}}]},
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "t", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
            },
            {"id": "c", "model": "kimi", "choices": [{"index": 0, "delta": {"content": "after"}}]},
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
    )
    lifecycle = [
        (t, d["index"]) for t, d in events if t in ("content_block_start", "content_block_stop")
    ]
    # three blocks: text(0), tool(1), text(2) — each closed before the next opens
    assert lifecycle == [
        ("content_block_start", 0),
        ("content_block_stop", 0),
        ("content_block_start", 1),
        ("content_block_stop", 1),
        ("content_block_start", 2),
        ("content_block_stop", 2),
    ]
    # the resumed text delta targets the new block (index 2), not the stopped one (index 0)
    after = [d for t, d in events if t == "content_block_delta" and d["index"] == 2]
    assert after[0]["delta"] == {"type": "text_delta", "text": "after"}


def test_stream_fabricates_output_tokens_when_usage_missing():
    events = _drive(
        [
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {"content": "Hello world"}}],
            },
            {
                "id": "c",
                "model": "kimi",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
    )
    delta = next(d for t, d in events if t == "message_delta")
    # no usage chunk arrived -> chars/4 estimate over "Hello world" (11 chars)
    assert delta["usage"]["output_tokens"] == 2
    assert "input_tokens" not in delta["usage"]


# ---------------------------------------------------------------------------
# TranslateProxy — live end-to-end against a fake OpenAI upstream
# ---------------------------------------------------------------------------


class _FakeOpenAIState:
    def __init__(self, response):
        self.response = response  # ("json", obj) | ("error", (status, obj)) | ("stream", [chunks])
        self.last_body = None
        self.last_path = None
        self.last_auth = None
        self.last_accept_encoding = None


class _FakeOpenAI:
    """A fake OpenAI Chat Completions endpoint that records the request and scripts a reply."""

    def __init__(self, state):
        self.state = state
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_openai_handler(state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def _make_openai_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            state.last_body = json.loads(self.rfile.read(length)) if length else {}
            state.last_path = self.path
            state.last_auth = self.headers.get("Authorization")
            state.last_accept_encoding = self.headers.get("Accept-Encoding")
            kind, payload = state.response
            if kind == "stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for chunk in payload:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            status, obj = payload if kind == "error" else (200, payload)
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def _anthropic_request(proxy, body, path="/v1/messages"):
    return urllib.request.Request(
        proxy.base_url + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": "sk-123",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )


def test_translate_proxy_non_streaming_text():
    state = _FakeOpenAIState(
        response=(
            "json",
            {
                "id": "chatcmpl-x",
                "model": "kimi-k2.7-code",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )
    )
    with (
        _FakeOpenAI(state) as upstream,
        TranslateProxy(upstream.base_url, model="kimi-k2.7-code") as proxy,
    ):
        request = _anthropic_request(
            proxy,
            {
                "model": "claude-x",
                "system": "be brief",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "t", "input_schema": {"type": "object"}}],
            },
        )
        with urllib.request.urlopen(request) as response:
            body = json.loads(response.read())
    # request was rewritten to the OpenAI surface, auth swapped, model overridden
    assert state.last_path == "/v1/chat/completions"
    assert state.last_auth == "Bearer sk-123"
    assert state.last_body["model"] == "kimi-k2.7-code"
    assert state.last_body["messages"][0] == {"role": "system", "content": "be brief"}
    assert state.last_body["tools"][0]["function"]["name"] == "t"
    # identity encoding is forced — we re-parse the body, so a gzip response would break us
    assert state.last_accept_encoding == "identity"
    # response was translated back to Anthropic shape
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 5


def test_translate_proxy_streaming_tool_call():
    chunks = [
        {"id": "c", "model": "kimi", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {
            "id": "c",
            "model": "kimi",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "c",
            "model": "kimi",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Paris"}'}}]
                    },
                }
            ],
        },
        {
            "id": "c",
            "model": "kimi",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    ]
    state = _FakeOpenAIState(response=("stream", chunks))
    with _FakeOpenAI(state) as upstream, TranslateProxy(upstream.base_url, model="kimi") as proxy:
        request = _anthropic_request(
            proxy, {"messages": [{"role": "user", "content": "weather in Paris?"}], "stream": True}
        )
        with urllib.request.urlopen(request) as response:
            assert response.headers.get("Content-Type") == "text/event-stream"
            raw = response.read()
    # upstream saw a streaming OpenAI request
    assert state.last_body["stream"] is True
    assert state.last_body["stream_options"] == {"include_usage": True}
    events = _parse_raw_sse(raw)
    types = [t for t, _ in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    start = next(d for t, d in events if t == "content_block_start")
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "get_weather"
    fragments = "".join(
        d["delta"]["partial_json"]
        for t, d in events
        if t == "content_block_delta" and d["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(fragments) == {"city": "Paris"}


def test_translate_proxy_translates_upstream_error():
    state = _FakeOpenAIState(
        response=(
            "error",
            (
                400,
                {"error": {"type": "invalid_request_error", "message": "function name is invalid"}},
            ),
        )
    )
    with _FakeOpenAI(state) as upstream, TranslateProxy(upstream.base_url) as proxy:
        request = _anthropic_request(proxy, {"messages": [{"role": "user", "content": "hi"}]})
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read())
            assert body["type"] == "error"
            assert body["error"]["type"] == "invalid_request_error"
            assert "function name is invalid" in body["error"]["message"]
        else:
            raise AssertionError("expected a 400 from the proxy")


def test_translate_proxy_rewrites_path_with_query_string():
    # Claude Code appends `?beta=true` to /v1/messages. The proxy must still rewrite to the
    # OpenAI endpoint (dropping the query) — otherwise the request hits the upstream's broken
    # Anthropic surface and auth fails.
    state = _FakeOpenAIState(
        response=(
            "json",
            {
                "id": "c",
                "model": "kimi",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
    )
    with _FakeOpenAI(state) as upstream, TranslateProxy(upstream.base_url, model="kimi") as proxy:
        request = _anthropic_request(
            proxy, {"messages": [{"role": "user", "content": "hi"}]}, path="/v1/messages?beta=true"
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
    assert state.last_path == "/v1/chat/completions"


def _ok_json_state():
    return _FakeOpenAIState(
        response=(
            "json",
            {
                "id": "c",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
    )


def test_translate_proxy_config_key_overrides_stale_client_auth():
    # Claude Code can send a good x-api-key AND a stale Authorization (a cached OAuth token).
    # The config-resolved key must win, or the wrong token is forwarded and the upstream 401s.
    state = _ok_json_state()
    with (
        _FakeOpenAI(state) as upstream,
        TranslateProxy(upstream.base_url, api_key="sk-config") as proxy,
    ):
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": "sk-client",
                "Authorization": "Bearer sk-stale-oauth",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
    assert state.last_auth == "Bearer sk-config"


def test_translate_proxy_falls_back_to_client_key_without_config_key():
    # With no configured key, relay the client's x-api-key (and never the stale Authorization).
    state = _ok_json_state()
    with _FakeOpenAI(state) as upstream, TranslateProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": "sk-client",
                "Authorization": "Bearer sk-stale-oauth",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
    assert state.last_auth == "Bearer sk-client"


def test_translate_proxy_count_tokens_short_circuits():
    state = _FakeOpenAIState(response=("json", {}))  # must NOT be reached
    with _FakeOpenAI(state) as upstream, TranslateProxy(upstream.base_url) as proxy:
        request = _anthropic_request(
            proxy,
            {"messages": [{"role": "user", "content": "hello world"}]},
            path="/v1/messages/count_tokens",
        )
        with urllib.request.urlopen(request) as response:
            body = json.loads(response.read())
    assert body["input_tokens"] >= 1
    assert state.last_path is None  # upstream was never contacted


def _parse_raw_sse(raw):
    events = []
    for blob in raw.decode().split("\n\n"):
        blob = blob.strip()
        if not blob:
            continue
        lines = blob.split("\n")
        event_type = lines[0].split("event:", 1)[1].strip()
        data = json.loads(lines[1].split("data:", 1)[1].strip())
        events.append((event_type, data))
    return events


# ---------------------------------------------------------------------------
# Responses <-> Chat Completions translation (codex harness)
# ---------------------------------------------------------------------------


def _reader(data: bytes):
    """A ``response.read(n)``-style callable over a fixed byte buffer."""
    buffer = {"bytes": data}

    def read(size):
        chunk = buffer["bytes"][:size]
        buffer["bytes"] = buffer["bytes"][size:]
        return chunk

    return read


def _chat_sse(*chunks) -> bytes:
    """A Chat Completions SSE byte stream from JSON chunk dicts plus a terminal ``[DONE]``."""
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _parse_responses_sse(frames: bytes) -> list:
    """Parse Responses-API SSE frames into ``[(event_type, data_dict), ...]``."""
    events = []
    for block in frames.decode().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        events.append((event_type, data))
    return events


def test_responses_to_chat_request_instructions_and_input():
    chat = responses_to_chat_request(
        {"model": "m", "instructions": "be terse", "input": "hello", "max_output_tokens": 128}
    )
    assert chat["model"] == "m"
    assert chat["stream"] is True
    assert chat["stream_options"] == {"include_usage": True}
    assert chat["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
    ]
    assert chat["max_tokens"] == 128


def test_responses_to_chat_request_input_items_and_tools():
    chat = responses_to_chat_request(
        {
            "instructions": "sys",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
                {"type": "function_call", "call_id": "c1", "name": "run", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
                {"type": "reasoning", "summary": []},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "run",
                    "description": "d",
                    "parameters": {"type": "object"},
                }
            ],
            "tool_choice": "auto",
        }
    )
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant", "tool"]
    assistant = chat["messages"][2]
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert assistant["tool_calls"][0]["function"] == {"name": "run", "arguments": "{}"}
    assert chat["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "done"}
    assert chat["tools"] == [
        {
            "type": "function",
            "function": {"name": "run", "description": "d", "parameters": {"type": "object"}},
        }
    ]
    assert chat["tool_choice"] == "auto"
    # The reasoning item is dropped (no extra message).
    assert len(chat["messages"]) == 4


def test_translate_chat_stream_text():
    blob = _chat_sse(
        {"choices": [{"delta": {"content": "PO"}}]},
        {"choices": [{"delta": {"content": "NG"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
    )
    events = _parse_responses_sse(
        b"".join(translate_chat_stream(_reader(blob), model="deepseek-v4-pro"))
    )
    types = [event for event, _ in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_item.added" in types
    deltas = [data["delta"] for event, data in events if event == "response.output_text.delta"]
    assert "".join(deltas) == "PONG"
    done = next(data for event, data in events if event == "response.output_text.done")
    assert done["text"] == "PONG"
    completed = next(data for event, data in events if event == "response.completed")
    assert completed["response"]["output"][0]["content"][0]["text"] == "PONG"
    assert completed["response"]["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
        "total_tokens": 4,
    }


def test_translate_chat_stream_tool_call():
    blob = _chat_sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "run", "arguments": '{"cmd":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    events = _parse_responses_sse(b"".join(translate_chat_stream(_reader(blob), model="m")))
    added = [
        data
        for event, data in events
        if event == "response.output_item.added" and data["item"]["type"] == "function_call"
    ]
    assert added[0]["item"]["call_id"] == "call_1"
    assert added[0]["item"]["name"] == "run"
    done = next(data for event, data in events if event == "response.function_call_arguments.done")
    assert done["arguments"] == '{"cmd":"ls"}'
    completed = next(data for event, data in events if event == "response.completed")
    function_item = completed["response"]["output"][0]
    assert function_item["type"] == "function_call"
    assert function_item["arguments"] == '{"cmd":"ls"}'


class _FakeChatUpstream:
    """Replies to ``POST /chat/completions`` with a fixed Chat Completions SSE stream."""

    def __init__(self, chunks):
        self.last_body = None
        self.last_path = None
        self._chunks = chunks
        handler = _make_chat_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def _make_chat_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            state.last_body = json.loads(self.rfile.read(length))
            state.last_path = self.path
            body = (
                b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in state._chunks)
                + b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def test_responses_proxy_translates_text_end_to_end():
    chunks = [
        {"choices": [{"delta": {"content": "PONG"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    with _FakeChatUpstream(chunks) as upstream, ResponsesToChatProxy(upstream.base_url) as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/responses",
            data=json.dumps(
                {"model": "deepseek-v4-pro", "instructions": "sys", "input": "ping", "stream": True}
            ).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-x"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            frames = response.read()

    # The upstream received a translated Chat Completions request at /chat/completions.
    assert upstream.last_path == "/chat/completions"
    assert [m["role"] for m in upstream.last_body["messages"]] == ["system", "user"]
    assert upstream.last_body["stream"] is True
    # codex sees the Responses-API SSE sequence carrying the text.
    events = _parse_responses_sse(frames)
    types = [event for event, _ in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    deltas = [data["delta"] for event, data in events if event == "response.output_text.delta"]
    assert "".join(deltas) == "PONG"


def test_responses_proxy_get_models_returns_empty_without_upstream():
    # codex's GET /models metadata probe is answered locally with a valid empty list — never
    # forwarded — so an unreachable upstream is irrelevant and codex gets a clean response.
    with ResponsesToChatProxy("http://127.0.0.1:1/v1") as proxy:
        request = urllib.request.Request(
            proxy.base_url + "/models?client_version=0.141.0", method="GET"
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"models": []}
