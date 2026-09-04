"""M4 fault-injection harness — the two-goal contract end-to-end (design §6.2).

Unlike :mod:`tests.test_failover`, which drives :class:`agedum.proxy.FailoverProxy`
against a hand-built route table, this harness exercises the **real launch wiring**:
a mix-like launcher config goes through :func:`agedum.provider.failover_spec` (via
``_maybe_failover_proxy``, the same context manager ``_run_config`` uses) and
:func:`agedum.provider.build_launch`; the simulated opencode client then reads the
emitted ``OPENCODE_CONFIG_CONTENT``, finds the rewritten
``provider.<id>.options.baseURL`` and posts a chat-completions request to the proxy —
the full path launch → baseURL rewrite → proxy → walled primary → fallback rung,
with no real quota spent.

The two-goal contract asserted here:

1. the request **lands on the fallback** (the SSE body carries the fallback stub's
   marker, so the answer provably came from the rung, not the primary);
2. the caller sees **one error, not five** — on the success path the wall never
   reaches the caller at all (opencode never enters its same-model retry loop), and
   on an exhausted chain the proxy collapses every internal wall into exactly one
   caller-visible error, returned verbatim.
"""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agedum.cli.main import _maybe_failover_proxy
from agedum.provider import build_launch

# ---------------------------------------------------------------------------
# Scripted stub upstreams
# ---------------------------------------------------------------------------


class _WallUpstream:
    """A provider that is walled on admission: every POST gets one fixed error.

    Records ``(path, headers, body)`` per request — the harness counts hits to
    prove the caller never saw the wall and the pin skips re-hitting it.
    """

    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = json.dumps(body).encode()
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
                stub.requests.append((self.path, dict(self.headers.items()), body))
                payload = stub.body
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _respond

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


class _SSEFallbackUpstream:
    """A healthy rung: streams a canned chat-completions SSE answer.

    The marker content is how the harness proves the caller's answer came from
    this rung and not from the walled primary.
    """

    MARKER = b"FALLBACK-STUB-OK"
    FRAMES = [
        b'data: {"id":"fb","choices":[{"delta":{"content":"FALLBACK-STUB-OK"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

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
                stub.requests.append((self.path, dict(self.headers.items()), body))
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


# ---------------------------------------------------------------------------
# Launch wiring + simulated opencode client
# ---------------------------------------------------------------------------

# The detect lists mirror the shipped oc/mix.json block; they are a copy, not a
# pin — if the shipped block changes, update here so the harness keeps injecting
# the shapes the proxy is actually configured to catch.
_MIX_DETECT = {
    "status": [429, 402],
    "messages": [
        "usage limit",
        "quota",
        "rate limit",
        "insufficient balance",
        "arrears",
        "image",
        "modalit",
        "media",
    ],
}


def _launcher_config(wall_url, provider_defs, chains, vision):
    """A mix-like opencode launcher whose providerDefs point at the stub upstreams."""
    return {
        "harness": "opencode",
        "requiredEnv": ["KIMI_API_KEY", "GLM_API_KEY", "DEEPSEEK_API_KEY"],
        "config": {
            "model": "kimi-coding/k3",
            "providerDef": provider_defs,
            "opencodeConfig": {
                "provider": {
                    "kimi-coding": {"models": {"k3": {"name": "Kimi K3"}}},
                    "glm": {"models": {"glm-5.3": {"name": "GLM"}}},
                    "deepseek": {"models": {"deepseek-v4-flash-vision-exp": {"name": "DS"}}},
                },
                "agent": {"main-k3": {"mode": "primary", "model": "kimi-coding/k3"}},
            },
        },
        "failover": {
            "detect": _MIX_DETECT,
            "maxWalk": 3,
            "vision": vision,
            "chains": chains,
        },
    }


@contextmanager
def _launched(config, base_env):
    """The real launch span: failover proxy live, launch built, config doc emitted."""
    with _maybe_failover_proxy(config, base_env) as failover:
        launch = build_launch(config, base_env, failover=failover)
        yield launch


def _opencode_post(launch, provider_id, body):
    """The opencode leg: post to the provider's rewritten baseURL like the SDK would."""
    document = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
    base_url = document["provider"][provider_id]["options"]["baseURL"]
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-kimi"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _chat_body():
    return {"model": "k3", "messages": [{"role": "user", "content": "hi"}]}


def _kimi_glm_defs(wall_url, rung_url):
    return [
        {
            "id": "kimi-coding",
            "npm": "@ai-sdk/openai-compatible",
            "baseUrl": wall_url,
            "apiKeyEnv": "KIMI_API_KEY",
        },
        {
            "id": "glm",
            "npm": "@ai-sdk/openai-compatible",
            "baseUrl": rung_url,
            "apiKeyEnv": "GLM_API_KEY",
        },
    ]


_BASE_ENV = {
    "KIMI_API_KEY": "sk-kimi",
    "GLM_API_KEY": "sk-glm",
    "DEEPSEEK_API_KEY": "sk-ds",
}


# ---------------------------------------------------------------------------
# Goal 1 + goal 2, success path — one wall, land on the fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wall_status,wall_body",
    [
        # 429 + usage-limit text — the OpenAI/kimi admission-wall shape.
        (429, {"error": {"message": "You've reached your usage limit"}}),
        # 402 — the DeepSeek "Insufficient Balance" shape.
        (402, {"error": {"message": "Insufficient Balance"}}),
        # A non-listed status whose text matches — the wall-text rule.
        (400, {"error": {"message": "rate limit exceeded, please retry later"}}),
    ],
    ids=["status-429", "status-402", "wall-text"],
)
def test_walled_primary_lands_on_fallback_one_error_not_five(wall_status, wall_body, capsys):
    """The two-goal contract per wall shape: the request lands on the fallback and
    the caller sees one successful response — never the wall."""
    with _WallUpstream(wall_status, wall_body) as wall, _SSEFallbackUpstream() as rung:
        config = _launcher_config(
            wall.base_url,
            _kimi_glm_defs(wall.base_url, rung.base_url),
            chains={"kimi-coding/k3": ["glm/glm-5.3"]},
            vision={"kimi-coding/k3": True, "glm/glm-5.3": False},
        )
        with _launched(config, _BASE_ENV) as launch:
            status, body = _opencode_post(launch, "kimi-coding", _chat_body())

    # Goal 1: the request landed on the fallback — the marker proves the SSE body
    # came from the rung stub, not the walled primary.
    assert status == 200
    assert _SSEFallbackUpstream.MARKER in body

    # Goal 2: the caller saw exactly ONE response, a success — the wall died at the
    # proxy and opencode never enters its same-model retry loop.
    assert b"usage limit" not in body and b"Insufficient" not in body

    # The primary was hit exactly once (the wall), the rung once, with the rung's
    # auth and model id — not the primary's.
    assert len(wall.requests) == 1
    assert len(rung.requests) == 1
    rung_path, rung_headers, rung_body = rung.requests[0]
    assert rung_path == "/chat/completions"
    assert rung_headers["Authorization"] == "Bearer sk-glm"
    assert json.loads(rung_body)["model"] == "glm-5.3"

    # The stderr walk line — the user's only signal that a failover happened.
    assert "agedum failover: kimi-coding/k3 rung 0 (primary)" in capsys.readouterr().err


def test_session_costs_one_wall_not_five_per_request():
    """Across requests in one session the rung pin keeps the walled primary out of
    the path: one wall error total, every subsequent request still lands on the
    fallback — not one wall per request."""
    with _WallUpstream(429, {"error": {"message": "quota exceeded"}}) as wall:
        with _SSEFallbackUpstream() as rung:
            config = _launcher_config(
                wall.base_url,
                _kimi_glm_defs(wall.base_url, rung.base_url),
                chains={"kimi-coding/k3": ["glm/glm-5.3"]},
                vision={"kimi-coding/k3": True, "glm/glm-5.3": False},
            )
            with _launched(config, _BASE_ENV) as launch:
                statuses = [
                    _opencode_post(launch, "kimi-coding", _chat_body())[0] for _ in range(3)
                ]
                wall_hits_after = len(wall.requests)

    assert statuses == [200, 200, 200]
    assert wall_hits_after == 1
    assert len(rung.requests) == 3


def test_variant_request_walks_variant_chain(capsys):
    """An effort-carrying body resolves the ``@variant`` chain key end-to-end —
    the effort knob is stripped and the rung's own options applied (D5) — and the
    walk line names the variant chain."""
    with _WallUpstream(429, {"error": {"message": "usage limit reached"}}) as wall:
        with _SSEFallbackUpstream() as rung:
            config = _launcher_config(
                wall.base_url,
                _kimi_glm_defs(wall.base_url, rung.base_url),
                chains={
                    "kimi-coding/k3@high": ["glm/glm-5.3@low"],
                    "kimi-coding/k3": ["glm/glm-5.3"],
                },
                vision={"kimi-coding/k3": True, "glm/glm-5.3": False},
            )
            # Exact runtime-rung options override the base catalogue entry (D5).
            config["failover"]["rungOptions"] = {"glm/glm-5.3@low": {"reasoningEffort": "low"}}
            with _launched(config, _BASE_ENV) as launch:
                body = _chat_body()
                body["reasoning_effort"] = "high"
                status, response = _opencode_post(launch, "kimi-coding", body)

    assert status == 200
    assert _SSEFallbackUpstream.MARKER in response
    assert len(wall.requests) == 1
    _, _, rung_body = rung.requests[0]
    rung_json = json.loads(rung_body)
    # The variant chain was used, the origin's effort knob is gone, and the exact
    # runtime-rung options stand in for it.
    assert rung_json["model"] == "glm-5.3"
    assert "reasoning_effort" not in rung_json
    assert rung_json["reasoningEffort"] == "low"
    assert "agedum failover: kimi-coding/k3@high rung 0 (primary)" in (capsys.readouterr().err)


# ---------------------------------------------------------------------------
# Goal 2, exhaustion path — every internal wall collapses into one caller error
# ---------------------------------------------------------------------------


def test_exhausted_chain_caller_sees_one_error_verbatim(capsys):
    """A chain where every rung is walled too: the caller receives exactly ONE
    error — the last rung's, verbatim — never a cascade of the walls the proxy
    walked through."""
    with _WallUpstream(429, {"error": {"message": "You've reached your usage limit"}}) as wall:
        with _WallUpstream(429, {"error": {"message": "quota exceeded"}}) as rung1:
            with _WallUpstream(402, {"error": {"message": "Insufficient Balance"}}) as rung2:
                config = _launcher_config(
                    wall.base_url,
                    [
                        {
                            "id": "kimi-coding",
                            "npm": "@ai-sdk/openai-compatible",
                            "baseUrl": wall.base_url,
                            "apiKeyEnv": "KIMI_API_KEY",
                        },
                        {
                            "id": "glm",
                            "npm": "@ai-sdk/openai-compatible",
                            "baseUrl": rung1.base_url,
                            "apiKeyEnv": "GLM_API_KEY",
                        },
                        {
                            "id": "deepseek",
                            "npm": "@ai-sdk/openai-compatible",
                            "baseUrl": rung2.base_url,
                            "apiKeyEnv": "DEEPSEEK_API_KEY",
                        },
                    ],
                    chains={
                        "kimi-coding/k3": [
                            "glm/glm-5.3",
                            "deepseek/deepseek-v4-flash-vision-exp",
                        ]
                    },
                    vision={
                        "kimi-coding/k3": True,
                        "glm/glm-5.3": False,
                        "deepseek/deepseek-v4-flash-vision-exp": True,
                    },
                )
                with _launched(config, _BASE_ENV) as launch:
                    status, body = _opencode_post(launch, "kimi-coding", _chat_body())

    # One caller-visible error, and it is the LAST rung's — verbatim.
    assert status == 402
    assert b"Insufficient Balance" in body
    assert b"quota exceeded" not in body
    assert b"usage limit" not in body

    # The proxy walked the whole chain: primary + both rungs, one walk line each.
    stderr = capsys.readouterr().err
    assert "rung 0 (primary)" in stderr
    assert "rung 1 (glm/glm-5.3)" in stderr
    assert "rung 2 (deepseek/deepseek-v4-flash-vision-exp)" in stderr
