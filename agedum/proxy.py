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
streaming the response back unchanged (SSE-safe). The wrapper points the child's
``ANTHROPIC_BASE_URL`` at this proxy and tears it down when the child exits.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = self._maybe_fold(raw)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() not in ("host", "content-length")
        }
        headers["Content-Length"] = str(len(body))

        url = self.upstream.rstrip("/") + self.path
        request = urllib.request.Request(
            url, data=body or None, headers=headers, method=self.command
        )
        try:
            response = urllib.request.urlopen(request)  # noqa: S310 (trusted upstream URL)
        except urllib.error.HTTPError as exc:
            response = exc  # an HTTPError is itself a readable response
        except urllib.error.URLError as exc:
            self._send_error(502, f"agedum fold-proxy: upstream unreachable: {exc.reason}")
            return
        except (ConnectionError, http.client.HTTPException) as exc:
            # A mid-flight disconnect (RemoteDisconnected / IncompleteRead) is raised from
            # getresponse(), which urllib does *not* wrap in URLError — so it escapes the
            # clause above. Catch the connection/HTTP-level families here; otherwise one
            # dropped upstream socket crashes the handler thread with an unhandled traceback.
            self._send_error(502, f"agedum fold-proxy: upstream disconnected: {exc}")
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
            response.close()

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

    def _relay(self, response) -> None:
        """Stream the upstream response back, chunk-encoded (SSE-safe)."""
        status = getattr(response, "status", None) or response.getcode()
        self.send_response(status)
        for key, value in response.headers.items():
            lowered = key.lower()
            if lowered in _HOP_BY_HOP or lowered in ("content-length",):
                continue
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            self.wfile.write(f"{len(chunk):X}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_error(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": message}}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
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
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
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
