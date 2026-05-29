import json
import threading
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
