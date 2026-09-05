"""Failover proxy (M1) + `failover` config wiring (M2) tests.

The unit level exercises :class:`agedum.proxy.FailoverProxy` directly against scripted
stub upstreams — wall detection, the chain walk (variant lookup, vision restriction,
pin, maxWalk), per-rung rewriting, the one-error-not-five contract, SSE passthrough,
and exhaustion. The config level exercises :func:`agedum.provider.failover_spec` +
``build_launch`` validation and the baseURL rewrite, including the byte-identical
no-failover-key regression that is the rollback guarantee.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agedum.provider import (
    FailoverPlan,
    ProviderError,
    _apply_provider_def,
    _opencode_config_doc,
    build_launch,
    failover_spec,
)
from agedum.proxy import (
    OPENAI_CODEX_UPSTREAM,
    FailoverProxy,
    _body_has_image,
    _FailoverHandler,
    _sniff_variant,
    failover_route_base,
)

# ---------------------------------------------------------------------------
# Scripted stub upstreams
# ---------------------------------------------------------------------------


class _StubUpstream:
    """A scripted provider endpoint: pops one ``(status, body, headers)`` per request.

    Body may be bytes or str. When the script is exhausted, replies
    ``{"ok": <name>}`` so the serving rung is identifiable from the response body.
    Records ``(method, path, headers, body)`` per request.
    """

    def __init__(self, name="ok", script=()):
        self.name = name
        self.script = list(script)
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append((self.command, self.path, dict(self.headers.items()), body))
                if stub.script:
                    status, payload, headers = stub.script.pop(0)
                else:
                    status = 200
                    payload = json.dumps({"ok": stub.name}).encode()
                    headers = {}
                if isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _respond
            do_GET = _respond
            do_PUT = _respond
            do_OPTIONS = _respond
            do_HEAD = _respond

            def log_message(self, *args):
                pass

        return Handler

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


class _SSEUpstream:
    """A close-delimited SSE upstream: streams fixed frames with flushes between."""

    FRAMES = [b'data: {"a": 1}\n\n', b'data: {"b": 2}\n\n', b"data: [DONE]\n\n"]

    def __init__(self):
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                stub.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for frame in stub.FRAMES:
                    self.wfile.write(frame)
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        return Handler

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


class _MidStreamDropUpstream:
    """Sends 200 + one SSE frame, then dies without completing the body."""

    def __init__(self):
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                stub.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(b'data: {"partial": true}\n\n')
                self.wfile.flush()
                # Return without completing the stream: the socket closes mid-body.

            def log_message(self, *args):
                pass

        return Handler

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


# ---------------------------------------------------------------------------
# Spec/proxy/request helpers
# ---------------------------------------------------------------------------


def _route(upstream, api_key="", models=None, openai=False):
    models = models or {}
    keys_by_wire = {}
    for key, entry in models.items():
        keys_by_wire.setdefault(entry.get("id") or key, []).append(key)
    return {
        "openai": openai,
        "upstream": upstream,
        "api_key": api_key,
        "models": models,
        "keys_by_wire": keys_by_wire,
    }


def _three_routes(a, b, c):
    """p/m1 primary on A, rungs f1/r1 (vision false) and f2/r2 (vision true, own options)."""
    return {
        "p": _route(a.base_url, api_key="key-p", models={"m1": {"id": "m1", "options": {}}}),
        "f1": _route(b.base_url, api_key="key-f1", models={"r1": {"id": "r1", "options": {}}}),
        "f2": _route(
            c.base_url,
            api_key="key-f2",
            models={
                "r2": {
                    "id": "r2",
                    "options": {"thinking": {"type": "enabled", "effort": "low"}},
                }
            },
        ),
    }


VISION = {"p/m1": True, "f1/r1": False, "f2/r2": True}


def _spec(routes, chains, vision=VISION, *, status=(429, 402), max_walk=3, rung_options=None):
    messages = ("usage limit", "quota", "insufficient balance", "image")
    return {
        "status": list(status),
        "messages": list(messages),
        "max_walk": max_walk,
        "vision": dict(vision),
        "chains": dict(chains),
        "routes": routes,
        "rung_options": dict(rung_options or {}),
    }


def _post(base_url, path, body, headers=None):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def _chat_body(model="m1", **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


# ---------------------------------------------------------------------------
# Chain walk — the two-goal contract
# ---------------------------------------------------------------------------


def test_walk_lands_on_first_surviving_rung():
    with (
        _StubUpstream("p", [(429, "usage limit exceeded", {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1", "f2/r2"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 200
    assert json.loads(body) == {"ok": "f1"}
    # One error, not five: the walled primary saw exactly one attempt, and the walk
    # stopped at the first surviving rung.
    assert len(a.requests) == 1
    assert len(b.requests) == 1
    assert c.requests == []
    # The rung saw the rewritten request: rung model id + rung auth.
    method, path, headers, rung_body = b.requests[0]
    assert (method, path) == ("POST", "/chat/completions")
    assert json.loads(rung_body)["model"] == "r1"
    assert headers["Authorization"] == "Bearer key-f1"


def test_exhaustion_returns_last_error_verbatim():
    with (
        _StubUpstream("p", [(429, "primary limit", {})]) as a,
        _StubUpstream("f1", [(429, "f1 limit", {})]) as b,
        _StubUpstream("f2", [(429, "f2 limit", {"Retry-After": "13"})]) as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1", "f2/r2"]})
        with FailoverProxy(spec) as proxy:
            status, headers, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 429
    assert body == b"f2 limit"
    assert headers["Retry-After"] == "13"
    # Exactly one attempt per rung — the caller sees ONE error, not a retry storm.
    assert [len(u.requests) for u in (a, b, c)] == [1, 1, 1]


def test_unreachable_rung_walks_on():
    with _StubUpstream("p", [(429, "limit", {})]) as a, _StubUpstream("f1") as b:
        routes = _three_routes(a, b, b)  # f2 slot placeholder, replaced below
        dead = _StubUpstream()  # a bound-but-dead port: nothing listens
        dead._server.server_close()
        routes["f2"] = _route(dead.base_url, api_key="key-f2", models={"r2": {"id": "r2"}})
        spec = _spec(routes, {"p/m1": ["f1/r1", "f2/r2"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 200
    assert json.loads(body) == {"ok": "f1"}
    assert len(a.requests) == 1


# ---------------------------------------------------------------------------
# Wall detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,expected_wall",
    [
        (429, "anything", True),  # status rule needs no text
        (402, "nothing readable", True),
        (400, "Insufficient Balance", True),  # text rule is case-insensitive
        (400, "you hit your USAGE LIMIT", True),
        (400, "this model does not support image input", True),  # modality safety net
        (400, "all fine here", False),  # non-listed 4xx without a message
        (401, "invalid api key", False),  # bad keys keep native semantics
        (500, "server boom", False),  # genuine 5xx keeps native retry semantics
    ],
)
def test_wall_classification(status, body, expected_wall):
    with (
        _StubUpstream("p", [(status, body, {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            got_status, _, got_body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    if expected_wall:
        assert got_status == 200  # the walk served the request
        assert len(b.requests) == 1
    else:
        # Verbatim passthrough: the caller sees the upstream error exactly as-is.
        assert got_status == status
        assert got_body == body.encode()
        assert b.requests == []


def test_wall_text_beyond_2kb_window_ignored():
    late = b"x" * 2048 + b"usage limit"
    with (
        _StubUpstream("p", [(400, late, {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 400
    assert body == late
    assert b.requests == []


def test_unknown_route_404():
    with _StubUpstream("p") as a, _StubUpstream("f1") as b, _StubUpstream("f2") as c:
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/nope/chat/completions", _chat_body())
    assert status == 404
    assert "no route" in body.decode()


# ---------------------------------------------------------------------------
# Variant sniffing + chain-key resolution
# ---------------------------------------------------------------------------


def test_sniff_variant_precedence():
    assert _sniff_variant({"reasoning_effort": "high", "reasoningEffort": "low"}) == "high"
    assert _sniff_variant({"reasoningEffort": "medium"}) == "medium"
    assert _sniff_variant({"thinking": {"type": "enabled", "effort": "low"}}) == "low"
    assert _sniff_variant({"thinking": {"type": "disabled"}}) == ""
    assert _sniff_variant({}) == ""


def test_missing_variant_defaults_to_high_before_bare_chain():
    with (
        _StubUpstream("p", script=[(429, "limit", {})] * 2) as a,
        _StubUpstream("high-rung") as b,
        _StubUpstream("bare-rung") as c,
    ):
        routes = {
            "p": _route(a.base_url, api_key="key-p", models={"m1": {"id": "m1", "options": {}}}),
            "f1": _route(b.base_url, api_key="key-f1", models={"r1": {"id": "r1"}}),
            "f2": _route(c.base_url, api_key="key-f2", models={"r2": {"id": "r2"}}),
        }
        chains = {"p/m1@high": ["f1/r1"], "p/m1": ["f2/r2"]}
        spec = _spec(routes, chains)
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(
                proxy.base_url,
                "/oc/p/chat/completions",
                _chat_body(reasoningEffort="high"),
            )
            assert status == 200
            assert json.loads(body) == {"ok": "high-rung"}
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
            assert status == 200
            assert json.loads(body) == {"ok": "high-rung"}


def test_wire_alias_resolves_through_keys_by_wire():
    # kimi-coding's k3/k3-low shape: two launcher keys share one wire id ("k3"); the
    # variant sniff separates them when only one has a chain. A high-variant request
    # resolves the bare p/k3-low chain — the bare key is the variant-agnostic fallback
    # (harmless in oc/mix, where k3 and k3-low carry identical chains).
    with _StubUpstream("p", script=[(429, "limit", {})] * 2) as a, _StubUpstream("low-rung") as b:
        routes = {
            "p": _route(
                a.base_url,
                api_key="key-p",
                models={
                    "k3": {"id": "k3", "options": {}},
                    "k3-low": {"id": "k3", "options": {}},
                },
            ),
            "f1": _route(b.base_url, api_key="key-f1", models={"r1": {"id": "r1"}}),
        }
        vision = {"p/k3": True, "p/k3-low": True, "f1/r1": True}
        spec = _spec(routes, {"p/k3-low": ["f1/r1"]}, vision=vision)
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(
                proxy.base_url,
                "/oc/p/chat/completions",
                _chat_body(model="k3", thinking={"type": "enabled", "effort": "low"}),
            )
            assert status == 200
            assert json.loads(body) == {"ok": "low-rung"}
            status, _, body = _post(
                proxy.base_url,
                "/oc/p/chat/completions",
                _chat_body(model="k3", thinking={"type": "enabled", "effort": "high"}),
            )
            assert status == 200
            assert json.loads(body) == {"ok": "low-rung"}  # bare-key fallback
    # The second request reused the pin earned by the first (same resolved chain
    # key + vision class), so the walled primary was contacted only once.
    assert len(a.requests) == 1
    assert len(b.requests) == 2


def test_wire_alias_prefers_the_matching_effort_chain(capsys):
    with (
        _StubUpstream("p", script=[(429, "limit", {})] * 2) as primary,
        _StubUpstream("rung") as rung,
    ):
        routes = {
            "p": _route(
                primary.base_url,
                api_key="key-p",
                models={
                    "k3": {
                        "id": "k3",
                        "options": {"thinking": {"type": "enabled", "effort": "high"}},
                    },
                    "k3-low": {
                        "id": "k3",
                        "options": {"thinking": {"type": "enabled", "effort": "low"}},
                    },
                },
            ),
            "f1": _route(rung.base_url, api_key="key-f1", models={"r1": {"id": "r1"}}),
        }
        spec = _spec(
            routes,
            {
                "p/k3@high": ["f1/r1"],
                "p/k3-low@low": ["f1/r1"],
            },
            vision={"p/k3": True, "p/k3-low": True, "f1/r1": True},
        )
        with FailoverProxy(spec) as proxy:
            assert (
                _post(
                    proxy.base_url,
                    "/oc/p/chat/completions",
                    _chat_body(model="k3", thinking={"type": "enabled", "effort": "low"}),
                )[0]
                == 200
            )
            assert (
                _post(
                    proxy.base_url,
                    "/oc/p/chat/completions",
                    _chat_body(model="k3", reasoning_effort="high"),
                )[0]
                == 200
            )

    walk_log = capsys.readouterr().err
    assert "p/k3-low@low rung 0 (primary)" in walk_log
    assert "p/k3@high rung 0 (primary)" in walk_log


def test_unmapped_model_forwards_transparently():
    with _StubUpstream("p") as a, _StubUpstream("f1") as b, _StubUpstream("f2") as c:
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(
                proxy.base_url, "/oc/p/chat/completions", _chat_body(model="other")
            )
    assert status == 200
    assert json.loads(body) == {"ok": "p"}
    assert b.requests == []


# ---------------------------------------------------------------------------
# Per-rung rewriting
# ---------------------------------------------------------------------------


def test_rung_rewrite_strips_effort_and_applies_rung_options():
    with (
        _StubUpstream("p", [(429, "limit", {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        routes = _three_routes(a, b, c)
        # Route the walk straight to f2/r2 (which carries thinking-low options).
        chains = {"p/m1": ["f2/r2"]}
        spec = _spec(routes, chains)
        with FailoverProxy(spec) as proxy:
            status, _, _ = _post(
                proxy.base_url,
                "/oc/p/chat/completions",
                _chat_body(reasoning_effort="high"),
            )
    assert status == 200
    _, _, headers, rung_body = c.requests[0]
    parsed = json.loads(rung_body)
    # Origin effort knob stripped; the rung model-key's own options applied (D5).
    assert "reasoning_effort" not in parsed
    assert parsed["thinking"] == {"type": "enabled", "effort": "low"}
    assert parsed["model"] == "r2"
    # Auth replaced per rung; the OAuth-mode account header never survives a rung hop.
    assert headers["Authorization"] == "Bearer key-f2"
    assert "ChatGPT-Account-Id" not in headers


def test_openai_primary_forwarded_verbatim_then_walk_rewrites():
    with _StubUpstream("openai", [(429, "usage limit reached", {})]) as a, _StubUpstream("f1") as b:
        routes = {
            "openai": _route(
                a.base_url,
                openai=True,
                models={"gpt-5.6-luna": {"id": "gpt-5.6-luna", "options": {}}},
            ),
            "f1": _route(b.base_url, api_key="key-f1", models={"r1": {"id": "r1"}}),
        }
        chains = {"openai/gpt-5.6-luna": ["f1/r1"]}
        spec = _spec(routes, chains, vision={"openai/gpt-5.6-luna": True, "f1/r1": True})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(
                proxy.base_url,
                "/oc/openai/responses",
                _chat_body(model="gpt-5.6-luna"),
                headers={"Authorization": "Bearer oauth-token", "ChatGPT-Account-Id": "acc-1"},
            )
    assert status == 200
    assert json.loads(body) == {"ok": "f1"}
    # The primary attempt kept the OAuth headers verbatim and the path shape mapped
    # /oc/openai/<rest> -> <upstream>/<rest>. (Header names are matched
    # case-insensitively: urllib canonicalises the casing on its way in.)
    method, path, headers, primary_body = a.requests[0]
    lower = {key.lower(): value for key, value in headers.items()}
    assert (method, path) == ("POST", "/responses")
    assert lower["authorization"] == "Bearer oauth-token"
    assert lower["chatgpt-account-id"] == "acc-1"
    assert json.loads(primary_body)["model"] == "gpt-5.6-luna"
    # The fallback rung got the rung auth instead, and dropped the account header.
    _, _, rung_headers, _ = b.requests[0]
    rung_lower = {key.lower(): value for key, value in rung_headers.items()}
    assert rung_lower["authorization"] == "Bearer key-f1"
    assert "chatgpt-account-id" not in rung_lower


def test_non_post_forwarded_verbatim_without_walk():
    with (
        _StubUpstream("p", [(429, "limit", {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            request = urllib.request.Request(proxy.base_url + "/oc/p/models")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    status = response.status
            except urllib.error.HTTPError as error:
                status = error.code
    # Even a 429 on a non-POST passes through: the walk is chat-admission-only.
    assert status == 429
    assert a.requests[0][0] == "GET"
    assert b.requests == []


def test_options_and_head_route_per_provider():
    # Regression: without overrides these verbs inherited the base class's
    # single-upstream _proxy(), whose `upstream` the failover handler never binds —
    # they must forward to the route's own upstream instead (and pass the 429
    # through, no walk).
    with (
        _StubUpstream("p", script=[(429, "limit", {})] * 2) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            for method in ("OPTIONS", "HEAD"):
                request = urllib.request.Request(proxy.base_url + "/oc/p/models", method=method)
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        status = response.status
                except urllib.error.HTTPError as error:
                    status = error.code
                assert status == 429
    assert [r[0] for r in a.requests] == ["OPTIONS", "HEAD"]
    assert b.requests == []


def test_variant_suffixed_rung_rewrites_to_base_model_key():
    # A rung carrying a @variant suffix validates against its base key and must
    # rewrite to the base model key's catalogue entry — not send the suffix upstream.
    with (
        _StubUpstream("p", [(429, "limit", {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f2/r2@low"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 200
    assert json.loads(body) == {"ok": "f2"}
    _, _, headers, rung_body = c.requests[0]
    parsed = json.loads(rung_body)
    assert parsed["model"] == "r2"  # not "r2@low"
    assert parsed["thinking"] == {"type": "enabled", "effort": "low"}
    assert headers["Authorization"] == "Bearer key-f2"


def test_exact_variant_rung_options_keep_low_and_high_distinct():
    with _StubUpstream("p", [(429, "limit", {})] * 2) as primary, _StubUpstream("rung") as rung:
        routes = {
            "p": _route(
                primary.base_url,
                api_key="key-p",
                models={"m1": {"id": "m1", "options": {}}},
            ),
            "f1": _route(
                rung.base_url,
                api_key="key-f1",
                models={
                    "gpt": {
                        "id": "gpt-5.6-luna",
                        "options": {"thinking": {"type": "enabled", "effort": "catalogue"}},
                    }
                },
            ),
        }
        chains = {
            "p/m1@low": ["f1/gpt@low"],
            "p/m1@high": ["f1/gpt@high"],
        }
        vision = {"p/m1": True, "f1/gpt": True}
        rung_options = {
            "f1/gpt@low": {"reasoning_effort": "low"},
            "f1/gpt@high": {"reasoningEffort": "high"},
        }
        spec = _spec(routes, chains, vision, rung_options=rung_options)
        with FailoverProxy(spec) as proxy:
            status, _, _ = _post(
                proxy.base_url,
                "/oc/p/chat/completions",
                _chat_body(thinking={"type": "enabled", "effort": "low"}),
            )
            assert status == 200
            status, _, _ = _post(
                proxy.base_url,
                "/oc/p/chat/completions",
                _chat_body(reasoningEffort="high"),
            )
            assert status == 200

    low_body, high_body = [json.loads(request[3]) for request in rung.requests]
    assert low_body["model"] == high_body["model"] == "gpt-5.6-luna"
    assert "thinking" not in low_body
    assert low_body["reasoning_effort"] == "low"
    assert "reasoning_effort" not in high_body
    assert high_body["reasoningEffort"] == "high"


# ---------------------------------------------------------------------------
# Rung pin (D6)
# ---------------------------------------------------------------------------


def test_pin_skips_walled_primary_on_subsequent_requests():
    with (
        _StubUpstream("p", [(429, "limit", {}), (429, "limit", {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            for _ in range(2):
                status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
                assert status == 200
                assert json.loads(body) == {"ok": "f1"}
    assert len(a.requests) == 1  # the walled primary was never re-hit
    assert len(b.requests) == 2


def test_pin_is_per_vision_class():
    with (
        _StubUpstream("p", script=[(429, "limit", {})] * 4) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1", "f2/r2"]})
        with FailoverProxy(spec) as proxy:
            text_body = _chat_body()
            image_body = _chat_body(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                        ],
                    }
                ]
            )
            # Text request: walks f1 (vision false but text-eligible) and pins it.
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", text_body)
            assert (status, json.loads(body)) == (200, {"ok": "f1"})
            # Image request: skips f1, lands on f2, pins the vision class separately.
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", image_body)
            assert (status, json.loads(body)) == (200, {"ok": "f2"})
            # Text pin: primary never contacted again, f1 serves.
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", text_body)
            assert (status, json.loads(body)) == (200, {"ok": "f1"})
            # Vision pin: neither primary nor the text-only rung contacted.
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", image_body)
            assert (status, json.loads(body)) == (200, {"ok": "f2"})
    # primary hit once per request class (text, image), never on the pinned replays.
    assert len(a.requests) == 2
    assert len(b.requests) == 2  # first text + pinned text replay
    assert len(c.requests) == 2  # first image + pinned image replay


def test_pinned_rung_failure_continues_the_walk():
    f1_script = [(200, json.dumps({"ok": "f1"}).encode(), {}), (429, "f1 limit", {})]
    with (
        _StubUpstream("p", script=[(429, "limit", {})] * 2) as a,
        _StubUpstream("f1", script=f1_script) as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1", "f2/r2"]})
        with FailoverProxy(spec) as proxy:
            assert _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())[0] == 200
            # The pinned rung now walls: the walk starts there and continues onward.
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
            assert status == 200
            assert json.loads(body) == {"ok": "f2"}
    assert len(a.requests) == 1  # pinned: primary not re-hit
    assert len(b.requests) == 2  # pinned rung was tried and walled
    assert len(c.requests) == 1


# ---------------------------------------------------------------------------
# Vision scan + vision-restricted walk
# ---------------------------------------------------------------------------


def test_body_has_image_shapes():
    assert _body_has_image(
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]}
    )
    assert _body_has_image(
        {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": ""}]}]}
    )
    # An image surfaced inside a tool result re-classifies correctly.
    assert _body_has_image(
        {
            "messages": [
                {
                    "role": "tool",
                    "content": [
                        {"type": "tool_result", "content": [{"type": "image_url", "image_url": {}}]}
                    ],
                }
            ]
        }
    )
    # All-strings fast path: nothing to inspect.
    assert not _body_has_image({"messages": [{"role": "user", "content": "hi"}]})
    assert not _body_has_image({"messages": []})


def test_image_request_skips_text_only_rungs():
    with (
        _StubUpstream("p", script=[(429, "limit", {})] * 2) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1", "f2/r2"]})
        image_body = _chat_body(
            messages=[
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
            ]
        )
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", image_body)
            assert (status, json.loads(body)) == (200, {"ok": "f2"})
            # Text request walks the full chain unchanged: the text-only rung serves.
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
            assert (status, json.loads(body)) == (200, {"ok": "f1"})
    assert b.requests  # the text-only rung served the text request
    assert all(json.loads(r[3])["messages"][0]["content"] == "hi" for r in b.requests)


def test_chain_without_vision_rung_exhausts_on_image_request():
    with (
        _StubUpstream("p", [(429, "primary limit", {})]) as a,
        _StubUpstream("f1") as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        image_body = _chat_body(
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]
        )
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", image_body)
    # No vision:true rung exists: the walk exhausts and the primary's error passes
    # through — the correct answer when no vision-capable fallback exists.
    assert status == 429
    assert body == b"primary limit"
    assert b.requests == []


# ---------------------------------------------------------------------------
# Walk cap
# ---------------------------------------------------------------------------


def test_max_walk_caps_rungs_tried():
    with (
        _StubUpstream("p", [(429, "primary limit", {})]) as a,
        _StubUpstream("f1", [(429, "f1 limit", {})]) as b,
        _StubUpstream("f2") as c,
    ):
        spec = _spec(
            _three_routes(a, b, c),
            {"p/m1": ["f1/r1", "f2/r2"]},
            max_walk=1,
        )
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 429
    assert body == b"f1 limit"
    assert c.requests == []  # the cap stopped the walk after one rung


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


def test_sse_passthrough_byte_identical():
    with _SSEUpstream() as a, _StubUpstream("f1") as b, _StubUpstream("f2") as c:
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            status, headers, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    assert status == 200
    assert "text/event-stream" in headers["Content-Type"]
    assert body == b"".join(_SSEUpstream.FRAMES)


def test_mid_stream_death_passes_through_without_walk():
    with _MidStreamDropUpstream() as a, _StubUpstream("f1") as b, _StubUpstream("f2") as c:
        spec = _spec(_three_routes(a, b, c), {"p/m1": ["f1/r1"]})
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/p/chat/completions", _chat_body())
    # v1 is admission-time only: the broken stream passes through, no walk.
    assert status == 200
    assert body.startswith(b'data: {"partial": true}\n\n')
    assert b.requests == []


# ---------------------------------------------------------------------------
# Path-prefix constraint (M0, notes/05)
# ---------------------------------------------------------------------------


def test_route_prefix_dodges_codex_url_rewrite():
    # opencode's OAuth fetch wrapper rewrites any request whose pathname contains
    # /v1/responses or /chat/completions to chatgpt.com (codex.ts:417-420) — the
    # route prefix must never carry either substring, or OAuth-mode chat requests
    # silently bypass the proxy.
    assert failover_route_base("openai") == "/oc/openai"
    for forbidden in ("/v1/responses", "/chat/completions"):
        assert forbidden not in failover_route_base("openai")
        assert forbidden not in failover_route_base("kimi-coding")
    with pytest.raises(ValueError):
        failover_route_base("v1/responses")
    with pytest.raises(ValueError):
        failover_route_base("chat/completions")


# ---------------------------------------------------------------------------
# failover_spec — parse + validation (M2)
# ---------------------------------------------------------------------------


def _mix_like_config(**failover_overrides):
    failover = {
        "detect": {"status": [429, 402], "messages": ["usage limit", "quota", "image"]},
        "maxWalk": 3,
        "vision": {
            "kimi-coding/k3": True,
            "kimi-coding/k3-low": True,
            "openai/gpt-5.6-luna": True,
        },
        "chains": {
            "kimi-coding/k3": ["openai/gpt-5.6-luna", "kimi-coding/k3-low"],
            "kimi-coding/k3-low": ["kimi-coding/k3"],
        },
    }
    failover.update(failover_overrides)
    return {
        "harness": "opencode",
        "requiredEnv": ["KIMI_API_KEY"],
        "config": {
            "model": "kimi-coding/k3",
            "providerDef": [
                {
                    "id": "kimi-coding",
                    "npm": "@ai-sdk/openai-compatible",
                    "baseUrl": "https://api.kimi.com/coding/v1",
                    "apiKeyEnv": "KIMI_API_KEY",
                }
            ],
            "opencodeConfig": {
                "provider": {
                    "kimi-coding": {
                        "models": {
                            "k3": {"name": "Kimi K3"},
                            "k3-low": {"id": "k3", "name": "Kimi K3 (low)"},
                        }
                    }
                },
                "agent": {"main-luna": {"mode": "primary", "model": "openai/gpt-5.6-luna"}},
            },
        },
        "failover": failover,
    }


def test_failover_spec_resolves_routes_and_prunes_openai_rungs():
    spec, warnings = failover_spec(_mix_like_config(), {"KIMI_API_KEY": "sk-kimi"})
    assert spec is not None
    # openai is a mapped primary (D1 PASS), pointing verbatim at the codex endpoint.
    assert spec["routes"]["openai"]["openai"] is True
    assert spec["routes"]["openai"]["upstream"] == OPENAI_CODEX_UPSTREAM
    kimi = spec["routes"]["kimi-coding"]
    assert kimi["upstream"] == "https://api.kimi.com/coding/v1"
    assert kimi["api_key"] == "sk-kimi"
    # The model catalogue carries the id override and the wire reverse map.
    assert kimi["models"]["k3-low"] == {"id": "k3", "options": {}}
    assert kimi["keys_by_wire"]["k3"] == ["k3", "k3-low"]
    # openai's model key is seeded from the agents' model references.
    assert "gpt-5.6-luna" in spec["routes"]["openai"]["models"]
    # The openai rung on k3's chain is pruned with a warning (D4); the rung after it
    # survives.
    assert spec["chains"]["kimi-coding/k3"] == ("kimi-coding/k3-low",)
    assert len(warnings) == 1
    assert "openai rung" in warnings[0]
    assert spec["chains"]["kimi-coding/k3-low"] == ("kimi-coding/k3",)


def test_failover_spec_preserves_full_rung_option_keys():
    options = {"thinking": {"type": "enabled", "effort": "low"}}
    config = _mix_like_config(rungOptions={"kimi-coding/k3-low": options})
    spec, _ = failover_spec(config, {"KIMI_API_KEY": "sk-kimi"})
    assert spec["rung_options"] == {"kimi-coding/k3-low": options}


@pytest.mark.parametrize(
    "rung_options,match",
    [
        ([], "rungOptions"),
        ({"": {}}, "non-empty"),
        ({"nope/no-model@low": {}}, "nope/no-model"),
        ({"openai/gpt-5.6-luna@low": []}, "JSON object"),
        ({"openai/gpt-5.6-luna@low": {"reasoning_effort": "low"}}, "unused"),
    ],
)
def test_failover_spec_rejects_invalid_rung_options(rung_options, match):
    with pytest.raises(ProviderError, match=match):
        failover_spec(_mix_like_config(rungOptions=rung_options), {"KIMI_API_KEY": "sk"})


def test_failover_spec_retains_effort_rungs_with_the_same_base_model():
    config = _mix_like_config(
        chains={
            "kimi-coding/k3": ["kimi-coding/k3-low@low", "kimi-coding/k3-low@high"],
            "kimi-coding/k3-low": ["kimi-coding/k3"],
        }
    )
    spec, _ = failover_spec(config, {"KIMI_API_KEY": "sk"})
    assert spec["chains"]["kimi-coding/k3"] == (
        "kimi-coding/k3-low@low",
        "kimi-coding/k3-low@high",
    )


def test_failover_spec_rejects_unknown_rung_with_the_offending_key():
    config = _mix_like_config(
        chains={"kimi-coding/k3": ["nope/no-model"], "kimi-coding/k3-low": ["kimi-coding/k3"]}
    )
    with pytest.raises(ProviderError, match="nope/no-model"):
        failover_spec(config, {"KIMI_API_KEY": "sk"})


def test_failover_spec_rejects_unknown_model_key():
    config = _mix_like_config(
        chains={"kimi-coding/k3": ["kimi-coding/nope"], "kimi-coding/k3-low": ["kimi-coding/k3"]}
    )
    with pytest.raises(ProviderError, match="kimi-coding/nope"):
        failover_spec(config, {"KIMI_API_KEY": "sk"})


def test_failover_spec_rejects_missing_vision_flag():
    config = _mix_like_config(
        chains={"kimi-coding/k3": ["kimi-coding/k3-low"], "kimi-coding/k3-low": ["kimi-coding/k3"]}
    )
    config["failover"]["vision"] = {"kimi-coding/k3": True}  # k3-low flag missing
    with pytest.raises(ProviderError, match="k3-low"):
        failover_spec(config, {"KIMI_API_KEY": "sk"})


def test_failover_spec_rejects_self_referential_chain():
    config = _mix_like_config(
        chains={"kimi-coding/k3": ["kimi-coding/k3"], "kimi-coding/k3-low": ["kimi-coding/k3"]}
    )
    with pytest.raises(ProviderError, match="own key"):
        failover_spec(config, {"KIMI_API_KEY": "sk"})


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"detect": {}}, "detect"),
        ({"detect": {"status": ["429"], "messages": ["x"]}}, "status"),
        (
            {"detect": {"status": [429], "messages": []}},
            "messages",
        ),
        ({"maxWalk": 0}, "maxWalk"),
        ({"vision": {}}, "vision"),
        ({"chains": {}}, "chains"),
        ({"chains": {"kimi-coding/k3": []}}, "non-empty list"),
    ],
)
def test_failover_spec_rejects_malformed_blocks(overrides, match):
    config = _mix_like_config(**overrides)
    if "chains" not in overrides:
        # k3's chain prunes to empty on its own; give both chains valid rungs so the
        # malformed-block error is the one raised.
        config["failover"]["chains"] = {
            "kimi-coding/k3": ["kimi-coding/k3-low"],
            "kimi-coding/k3-low": ["kimi-coding/k3"],
        }
        if "vision" not in overrides:
            config["failover"]["vision"] = {
                "kimi-coding/k3": True,
                "kimi-coding/k3-low": True,
            }
    with pytest.raises(ProviderError, match=match):
        failover_spec(config, {"KIMI_API_KEY": "sk"})


def test_failover_spec_absent_block_is_no_spec():
    config = _mix_like_config()
    del config["failover"]
    spec, warnings = failover_spec(config, {"KIMI_API_KEY": "sk"})
    assert spec is None
    assert warnings == []


def test_duplicate_provider_rungs_deduped():
    config = _mix_like_config(
        chains={
            "kimi-coding/k3": ["kimi-coding/k3-low", "kimi-coding/k3-low"],
            "kimi-coding/k3-low": ["kimi-coding/k3"],
        }
    )
    spec, _ = failover_spec(config, {"KIMI_API_KEY": "sk"})
    assert spec["chains"]["kimi-coding/k3"] == ("kimi-coding/k3-low",)


# ---------------------------------------------------------------------------
# build_launch wiring — the baseURL rewrite + rollback guarantee
# ---------------------------------------------------------------------------


def _env():
    return {"KIMI_API_KEY": "sk-kimi"}


def test_build_launch_rewrites_routed_base_urls_and_overlays_openai():
    config = _mix_like_config()
    plan = FailoverPlan(base_url="http://127.0.0.1:4321", routes=("kimi-coding", "openai"))
    launch = build_launch(config, _env(), failover=plan)
    document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    provider = document["provider"]
    assert provider["kimi-coding"]["options"]["baseURL"] == "http://127.0.0.1:4321/oc/kimi-coding"
    # The resolved apiKey value stays (the proxy rewrites Authorization per rung).
    assert provider["kimi-coding"]["options"]["apiKey"] == "sk-kimi"
    # The built-in openai provider is a pure options overlay — no npm, no key.
    assert provider["openai"]["options"]["baseURL"] == "http://127.0.0.1:4321/oc/openai"
    assert set(provider["openai"]) == {"options"}
    assert provider["openai"]["options"].keys() == {"baseURL"}


def test_build_launch_without_failover_is_byte_identical():
    # The rollback guarantee: without a `failover` key, the emitted config doc is
    # exactly what the pre-failover builder produced.
    config = _mix_like_config()
    del config["failover"]
    launch = build_launch(config, _env())
    document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    block = config["config"]
    expected = _opencode_config_doc(block)
    for provider_def in block["providerDef"]:
        expected = _apply_provider_def(expected, provider_def, _env())
    assert launch.env["OPENCODE_CONFIG_CONTENT"] == json.dumps(expected)
    # And nothing points at a failover route.
    assert "oc/kimi-coding" not in launch.env["OPENCODE_CONFIG_CONTENT"]
    assert "provider" not in document or "openai" not in document.get("provider", {})


def test_failover_spec_never_touches_a_non_opencode_harness():
    config = _mix_like_config()
    config["harness"] = "claude"
    with pytest.raises(ProviderError, match="opencode"):
        failover_spec(config, {})


# ---------------------------------------------------------------------------
# Chain resolution base fallback (Responses primaries have no effort knob)
# ---------------------------------------------------------------------------


def _resolver(chains):
    """A minimal ``_FailoverHandler`` stand-in: ``_resolve_chain`` reads only ``chains``."""
    handler = object.__new__(_FailoverHandler)
    handler.chains = chains
    return handler


def test_resolve_chain_base_fallback_finds_sole_suffixed_chain():
    # The openai Responses wire carries no effort knob at all: sniff → "" → defaults
    # high, so an @low-keyed chain is reachable only through the base fallback.
    route = _route("http://up", models={"gpt-5.6-sol": {"id": "gpt-5.6-sol"}})
    chains = {"openai/gpt-5.6-sol@low": ("f1/r1",)}
    assert _resolver(chains)._resolve_chain("openai", route, {"model": "gpt-5.6-sol"}) == (
        "openai/gpt-5.6-sol@low",
        ("f1/r1",),
    )


def test_resolve_chain_exact_variant_still_preferred_over_fallback():
    # Knob-less (defaults high) with both variants authored: @high wins in the main
    # lookup — the fallback never fires.
    route = _route("http://up", models={"m1": {"id": "m1"}})
    chains = {"p/m1@low": ("f1/r1",), "p/m1@high": ("f2/r2",)}
    assert _resolver(chains)._resolve_chain("p", route, {"model": "m1"}) == (
        "p/m1@high",
        ("f2/r2",),
    )


def test_resolve_chain_base_fallback_is_sorted_and_deterministic():
    # A variant with no authored chain (medium) misses both exact candidates; the
    # fallback picks the sorted-first surviving chain sharing the base.
    route = _route("http://up", models={"m1": {"id": "m1"}})
    chains = {"p/m1@low": ("f1/r1",), "p/m1@high": ("f2/r2",)}
    assert _resolver(chains)._resolve_chain(
        "p", route, {"model": "m1", "reasoning_effort": "medium"}
    ) == ("p/m1@high", ("f2/r2",))


def test_resolve_chain_base_fallback_needs_a_matching_base():
    # A model whose base has no chain at all stays unmapped: transparent forward.
    route = _route("http://up", models={"m1": {"id": "m1"}})
    chains = {"p/other@low": ("f1/r1",)}
    assert _resolver(chains)._resolve_chain("p", route, {"model": "m1"}) == ("", None)


# ---------------------------------------------------------------------------
# Translated rung hop — Responses primary → chat-completions rung
# ---------------------------------------------------------------------------


class _ChatSSEUpstream:
    """A healthy chat-completions rung: records requests, streams canned Chat SSE.

    One ``reasoning_content`` delta (the GLM Flash thinking shape) then the answer
    text and a usage frame — enough for the translated relay's full Responses
    sequence to be asserted.
    """

    MARKER = "TRANSLATED-STUB-OK"
    THINKING = "thinking out loud"
    FRAMES = (
        b'data: {"id":"c1","choices":[{"delta":{"reasoning_content":"thinking out loud"}}]}\n\n',
        b'data: {"id":"c1","choices":[{"delta":{"content":"TRANSLATED-STUB-OK"}}]}\n\n',
        b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}\n\n',
        b"data: [DONE]\n\n",
    )

    def __init__(self):
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append((self.command, self.path, dict(self.headers.items()), body))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for frame in stub.FRAMES:
                    self.wfile.write(frame)
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        return Handler

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


def _responses_body(model="gpt-5.6-sol", **extra):
    """A Responses-shaped request body — the openai/OAuth wire opencode actually sends."""
    return {
        "model": model,
        "instructions": "You are a helpful assistant.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "text": {"verbosity": "low"},
        "store": False,
        **extra,
    }


def _sse_events(raw: bytes) -> list[tuple[str, dict]]:
    """Parsed ``(event, data)`` pairs from a Responses SSE body."""
    events: list[tuple[str, dict]] = []
    for frame in raw.split(b"\n\n"):
        if not frame.strip():
            continue
        event_type = ""
        data = b"{}"
        for line in frame.split(b"\n"):
            if line.startswith(b"event: "):
                event_type = line[len(b"event: ") :].decode()
            elif line.startswith(b"data: "):
                data = line[len(b"data: ") :]
        events.append((event_type, json.loads(data)))
    return events


def _openai_routes(wall, rung):
    """The openai (Responses/OAuth) primary + one chat-completions rung."""
    return {
        "openai": _route(
            wall.base_url,
            openai=True,
            models={"gpt-5.6-sol": {"id": "gpt-5.6-sol", "options": {}}},
        ),
        "f1": _route(rung.base_url, api_key="key-f1", models={"r1": {"id": "r1", "options": {}}}),
    }


def test_translated_rung_speaks_chat_and_client_sees_responses_sse(capsys):
    with (
        _StubUpstream("openai", [(429, "usage limit exceeded", {})]) as wall,
        _ChatSSEUpstream() as rung,
    ):
        spec = _spec(
            _openai_routes(wall, rung),
            {"openai/gpt-5.6-sol@low": ["f1/r1"]},
            vision={"openai/gpt-5.6-sol": True, "f1/r1": True},
            rung_options={"f1/r1": {"reasoning_effort": "high"}},
        )
        with FailoverProxy(spec) as proxy:
            status, headers, body = _post(
                proxy.base_url,
                "/oc/openai/responses",
                _responses_body(),
                headers={"Authorization": "Bearer oauth-token", "ChatGPT-Account-Id": "acc-1"},
            )
    assert status == 200
    assert "text/event-stream" in headers["Content-Type"]

    # The rung hop arrived as a chat-completions request: system + flattened
    # messages body, rung model id, the rungOptions effort, SSE negotiated, rung
    # auth (no OAuth account header), and none of the Responses-only fields.
    method, path, rung_headers, rung_body = rung.requests[0]
    assert (method, path) == ("POST", "/chat/completions")
    rung_lower = {key.lower(): value for key, value in rung_headers.items()}
    assert rung_lower["authorization"] == "Bearer key-f1"
    assert rung_lower["accept"] == "text/event-stream"
    assert "chatgpt-account-id" not in rung_lower
    chat = json.loads(rung_body)
    assert chat["model"] == "r1"
    assert chat["messages"] == [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
    ]
    assert chat["reasoning_effort"] == "high"
    assert chat["stream"] is True
    for responses_only in ("input", "instructions", "text", "store"):
        assert responses_only not in chat

    # The client saw one full Responses SSE sequence — created → reasoning item →
    # message item → completed — with the rung's answer as the message text.
    events = _sse_events(body)
    assert [event for event, _ in events] == [
        "response.created",
        "response.output_item.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.output_item.done",
        "response.completed",
    ]
    completed = events[-1][1]["response"]
    assert completed["status"] == "completed"
    assert completed["model"] == "r1"
    assert completed["usage"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    reasoning_item, message_item = completed["output"]
    assert reasoning_item["type"] == "reasoning"
    assert reasoning_item["summary"][0]["text"] == _ChatSSEUpstream.THINKING
    assert message_item["type"] == "message"
    assert message_item["content"][0]["text"] == _ChatSSEUpstream.MARKER

    # The primary saw the Responses request verbatim (and wall'ed), exactly once.
    assert len(wall.requests) == 1
    primary_method, primary_path, _, primary_body = wall.requests[0]
    assert (primary_method, primary_path) == ("POST", "/responses")
    assert json.loads(primary_body)["input"]

    # The stderr walk lines — the user's only failover signal.
    walk_log = capsys.readouterr().err
    assert "agedum failover: openai/gpt-5.6-sol@low rung 0 (primary)" in walk_log
    assert "agedum failover: openai/gpt-5.6-sol@low rung 1 (f1/r1)" in walk_log


def test_translated_rung_pin_skips_walled_primary(capsys):
    with (
        _StubUpstream("openai", [(429, "usage limit exceeded", {})]) as wall,
        _ChatSSEUpstream() as rung,
    ):
        spec = _spec(
            _openai_routes(wall, rung),
            {"openai/gpt-5.6-sol@low": ["f1/r1"]},
            vision={"openai/gpt-5.6-sol": True, "f1/r1": True},
        )
        with FailoverProxy(spec) as proxy:
            for _ in range(2):
                status, _, body = _post(proxy.base_url, "/oc/openai/responses", _responses_body())
                assert status == 200
                assert _ChatSSEUpstream.MARKER.encode() in body
    assert len(wall.requests) == 1  # pinned: the walled primary was never re-hit
    assert [request[1] for request in rung.requests] == ["/chat/completions"] * 2
    # The walk logged the wall and the rung that answered — once; the pinned replay
    # never walked, so it stayed silent.
    assert capsys.readouterr().err.splitlines() == [
        "agedum failover: openai/gpt-5.6-sol@low rung 0 (primary)",
        "agedum failover: openai/gpt-5.6-sol@low rung 1 (f1/r1)",
    ]


def test_image_bearing_responses_request_walks_only_vision_rungs():
    with (
        _StubUpstream("openai", [(429, "limit", {})]) as wall,
        _StubUpstream("f1") as text_only,
        _ChatSSEUpstream() as vision_rung,
    ):
        routes = _openai_routes(wall, text_only)
        routes["f2"] = _route(vision_rung.base_url, api_key="key-f2", models={"r2": {"id": "r2"}})
        spec = _spec(
            routes,
            {"openai/gpt-5.6-sol@low": ["f1/r1", "f2/r2"]},
            vision={"openai/gpt-5.6-sol": True, "f1/r1": False, "f2/r2": True},
        )
        image_body = _responses_body(
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "look"},
                        {"type": "input_image", "image_url": "data:image/png;base64,x"},
                    ],
                }
            ]
        )
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/openai/responses", image_body)
    assert status == 200
    assert _ChatSSEUpstream.MARKER.encode() in body
    # The vision filter ran on the Responses shape: the text-only rung was skipped,
    # the vision rung got the translated hop.
    assert text_only.requests == []
    assert vision_rung.requests[0][1] == "/chat/completions"
    # The image part has no chat-completions equivalent in the MVP translator: the
    # text parts survive the flattening.
    assert json.loads(vision_rung.requests[0][3])["messages"] == [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "look"},
    ]


def test_translated_rung_exhaustion_returns_error_verbatim():
    with (
        _StubUpstream("openai", [(429, "usage limit exceeded", {})]) as wall,
        _StubUpstream("f1", [(402, "Insufficient Balance", {"Retry-After": "9"})]) as rung,
    ):
        spec = _spec(
            _openai_routes(wall, rung),
            {"openai/gpt-5.6-sol@low": ["f1/r1"]},
            vision={"openai/gpt-5.6-sol": True, "f1/r1": True},
        )
        with FailoverProxy(spec) as proxy:
            status, headers, body = _post(proxy.base_url, "/oc/openai/responses", _responses_body())
    # The translated rung's wall classified as today: the walk exhausted and the
    # last upstream error passed through verbatim.
    assert status == 402
    assert body == b"Insufficient Balance"
    assert headers["Retry-After"] == "9"
    assert rung.requests[0][1] == "/chat/completions"


def test_translated_rung_non_wall_error_passes_through_without_walk():
    with (
        _StubUpstream("openai", [(429, "limit", {})]) as wall,
        _StubUpstream("f1", [(401, "invalid api key", {})]) as rung,
    ):
        spec = _spec(
            _openai_routes(wall, rung),
            {"openai/gpt-5.6-sol@low": ["f1/r1"]},
            vision={"openai/gpt-5.6-sol": True, "f1/r1": True},
        )
        with FailoverProxy(spec) as proxy:
            status, _, body = _post(proxy.base_url, "/oc/openai/responses", _responses_body())
    # Bad keys keep native semantics even on a translated hop.
    assert status == 401
    assert body == b"invalid api key"
    assert len(wall.requests) == 1
