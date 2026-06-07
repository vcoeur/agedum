import contextlib
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agedum.proxy import FoldProxy, fold_system_messages

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


class _MidBodyDropUpstream:
    """Sends a valid 200 with a large Content-Length, a few body bytes, then closes.

    The status line and headers arrive intact, so the proxy's ``urlopen`` succeeds and
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
