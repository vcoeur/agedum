"""A localhost reverse proxy that normalizes Claude Code requests for strict
Anthropic-compat upstreams.

Some Anthropic-compatible endpoints (notably DeepSeek's ``/anthropic``) reject a
``system`` role inside the ``messages`` array — their schema accepts only ``user`` and
``assistant``. Claude Code, however, emits hook ``additionalContext`` (e.g. the
SessionStart memory reminder) as a ``system``-role message in ``messages``, *alongside*
the genuine top-level ``system`` prompt. The real Anthropic API and lenient compat
endpoints tolerate it; strict ones return ``400 unknown variant 'system'``.

:class:`FoldProxy` listens on an ephemeral ``127.0.0.1`` port, folds every
``system``-role message into the top-level ``system`` field (always API-valid: the
endpoint already accepts that field), and forwards the request to the real upstream,
streaming the response back close-delimited (SSE-safe). The wrapper points the child's
``ANTHROPIC_BASE_URL`` at this proxy and tears it down when the child exits.

Transport stance: a reverse proxy sees peers hang up constantly — an idle socket reaped
by the client, a generation interrupted mid-stream, a connection reset before the request
line is even read. Those are routine, not errors. :class:`_QuietThreadingHTTPServer`
absorbs the connection-level exception families in one place so they never surface as
stderr tracebacks, the proxy serves **one request per connection** (``Connection: close``,
so the client keeps no idle sockets to reset later), and the upstream hop uses
:mod:`http.client` directly, whose single exception family makes upstream failures uniform
to handle.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# Headers that must not be copied verbatim across a proxy hop (RFC 7230 §6.1), plus the
# framing headers we recompute ourselves.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _normalize_system(system: object) -> list:
    """Return the top-level ``system`` field as a list of content blocks."""
    if isinstance(system, str):
        return [{"type": "text", "text": system}] if system else []
    if isinstance(system, list):
        return list(system)
    return []


def _extract_text(content: object) -> str:
    """Flatten a message's ``content`` (string or block list) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def fold_system_messages(body: dict) -> dict:
    """Fold every ``system``-role entry in ``body['messages']`` into ``body['system']``.

    Returns a new dict with the offending messages removed and their text appended to
    the top-level ``system`` block list. If there is no ``messages`` list or no
    ``system``-role entry, the original object is returned unchanged (identity).
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body
    if not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
        return body

    system_blocks = _normalize_system(body.get("system"))
    kept: list = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            text = _extract_text(message.get("content"))
            if text:
                system_blocks.append({"type": "text", "text": text})
        else:
            kept.append(message)

    folded = dict(body)
    folded["messages"] = kept
    if system_blocks:
        folded["system"] = system_blocks
    return folded


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that treats a peer disconnect as routine, not an error.

    A reverse proxy's clients hang up all the time: an idle keep-alive socket reaped by
    the client (``ConnectionResetError`` before the request line is read), a streaming
    generation the user interrupts (``BrokenPipeError`` mid-response), a connection
    aborted mid-upload. The stdlib's default ``handle_error`` dumps a traceback for each
    of these. This is the single seam where every such teardown — wherever in the handler
    it is raised — is absorbed silently; anything that is *not* a connection-level error
    still falls through to the default handler.
    """

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        exception = sys.exc_info()[1]
        # ConnectionResetError / BrokenPipeError / ConnectionAbortedError are all
        # ConnectionError subclasses; a slow or vanished client can also raise
        # TimeoutError. For a proxy these are expected, not faults.
        if isinstance(exception, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _FoldHandler(BaseHTTPRequestHandler):
    """Forward each request to ``upstream`` after folding system-role messages.

    Subclassed per proxy instance so ``upstream`` is bound without globals.
    """

    upstream = ""
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        self._proxy()

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def _proxy(self) -> None:
        # One request per connection: the client then keeps no idle socket to reset later.
        self.close_connection = True

        raw = self._read_body()
        body = self._maybe_fold(raw)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() not in ("host", "content-length")
        }
        headers["Content-Length"] = str(len(body))

        upstream = urlsplit(self.upstream)
        path = upstream.path.rstrip("/") + self.path
        # The timeout bounds every socket op on the hop (connect + each read), so a dead
        # upstream cannot pin a handler thread forever. Generous on purpose: an SSE stream
        # only has to produce *some* bytes within the window, and the API pings well inside
        # five minutes — this is a hung-peer backstop, not a request deadline.
        connection = (
            http.client.HTTPSConnection(upstream.hostname, upstream.port, timeout=300)
            if upstream.scheme == "https"
            else http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=300)
        )
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            # Upstream unreachable, or dropped mid-flight (RemoteDisconnected /
            # IncompleteRead). http.client raises one predictable family for both, so a
            # single clause covers what urllib split across URLError and bare
            # ConnectionError. A live client still gets a clean 502 body.
            connection.close()
            self._send_error(502, f"agedum fold-proxy: upstream error: {exc}")
            return
        try:
            self._relay(response)
        except (OSError, http.client.HTTPException):
            # A mid-stream disconnect: the downstream client going away (BrokenPipeError /
            # ConnectionResetError from self.wfile.write) or the upstream dropping mid-body
            # (IncompleteRead from response.read). The status line and headers are already
            # sent, so a 502 is no longer possible — the response is in flight and the
            # connection is broken. Close the connection and stop quietly instead of letting
            # the exception crash the ThreadingHTTPServer handler thread.
            self.close_connection = True
        finally:
            connection.close()

    def _read_body(self) -> bytes:
        """Read the request body — ``Content-Length``-framed or ``chunked``.

        ``http.server`` does not decode chunked transfer coding itself; without this a
        chunked request would be forwarded body-less. The body is de-chunked here and
        re-framed with a recomputed ``Content-Length`` for the upstream hop
        (``Transfer-Encoding`` is hop-by-hop and never copied across).
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().split(b";", 1)[0].strip()
                size = int(size_line or b"0", 16)
                if size == 0:
                    # Consume the (empty) trailer section up to the final blank line.
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # the CRLF terminating the chunk data
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _maybe_fold(self, raw: bytes) -> bytes:
        """Fold system-role messages out of a JSON body; pass anything else through."""
        if not raw:
            return raw
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return raw
        if not isinstance(parsed, dict):
            return raw
        folded = fold_system_messages(parsed)
        if folded is parsed:
            return raw
        return json.dumps(folded).encode("utf-8")

    def _relay(self, response: http.client.HTTPResponse) -> None:
        """Stream the upstream response back, delimited by connection close (SSE-safe).

        We advertise ``Connection: close`` and forward the body straight through, so its
        end is the end of the connection — no manual chunked re-framing to get wrong, and
        no idle keep-alive socket for the client to reset afterwards.
        """
        self.send_response(response.status)
        for key, value in response.getheaders():
            lowered = key.lower()
            if lowered in _HOP_BY_HOP or lowered == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def _send_error(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": message}}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence the default stderr access log
        pass


class FoldProxy:
    """A running localhost proxy that folds system-role messages toward ``upstream``.

    Use as a context manager; :attr:`base_url` is the address to point
    ``ANTHROPIC_BASE_URL`` at while the ``with`` block is open.
    """

    def __init__(self, upstream: str) -> None:
        handler = type("_BoundFoldHandler", (_FoldHandler,), {"upstream": upstream})
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>`` — the ephemeral address the proxy bound to."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FoldProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
