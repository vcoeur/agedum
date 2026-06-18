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

:class:`ResponsesToChatProxy` is the codex-harness analog and does real protocol
translation. Recent codex (≥ the Feb-2026 removal of ``wire_api = "chat"``) speaks only the
OpenAI **Responses API**, but DeepSeek and most OpenAI-compatible providers serve only **Chat
Completions**. This proxy translates codex's ``POST /responses`` request into a
``/chat/completions`` request, forwards it to the real upstream, and translates the streamed
Chat Completions deltas back into the Responses-API SSE event sequence codex consumes (text +
function calls). The codex provider's ``base_url`` is pointed at it for the session.

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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
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


def read_http_body(rfile, headers) -> bytes:
    """Read a request body off ``rfile`` — ``Content-Length``-framed or ``chunked``.

    ``http.server`` does not decode chunked transfer coding itself; without this a chunked
    request would be forwarded body-less. A chunked body is de-chunked here so the caller can
    re-frame it with a recomputed ``Content-Length`` for the upstream hop
    (``Transfer-Encoding`` is hop-by-hop and never copied across). Shared by both proxies.
    """
    if "chunked" in (headers.get("Transfer-Encoding") or "").lower():
        chunks: list[bytes] = []
        while True:
            size_line = rfile.readline().split(b";", 1)[0].strip()
            size = int(size_line or b"0", 16)
            if size == 0:
                # Consume the (empty) trailer section up to the final blank line.
                while rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(chunks)
            chunks.append(rfile.read(size))
            rfile.readline()  # the CRLF terminating the chunk data
    length = int(headers.get("Content-Length") or 0)
    return rfile.read(length) if length else b""


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
        return read_http_body(self.rfile, self.headers)

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


# ---------------------------------------------------------------------------
# Responses API <-> Chat Completions translation (codex harness)
# ---------------------------------------------------------------------------


def _responses_message_text(content: object) -> str:
    """Flatten a Responses message ``content`` (string or part list) into plain text.

    Parts are ``{type: input_text|output_text|text, text: ...}`` (or bare strings); their
    ``text`` is concatenated. Non-text parts (images, etc.) are skipped — MVP is text-only.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _function_call_output_text(output: object) -> str:
    """The string content of a Responses ``function_call_output`` item → a Chat tool message."""
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        return _responses_message_text(output)
    return json.dumps(output)


def _append_input_messages(messages: list[dict], input_: object) -> None:
    """Append Responses ``input`` items to the Chat ``messages`` list, mapping item types.

    ``message`` → its role's Chat message (``developer`` → ``system``, hoisted to the front);
    consecutive ``function_call`` items → one assistant message with a ``tool_calls`` array
    (Chat requires all of a turn's calls in one message); ``function_call_output`` →
    a ``role: tool`` message keyed by ``tool_call_id``; ``reasoning`` items are dropped (the
    thinking-model reasoning round-trip is a deferred follow-up — see project note 01).
    """
    if input_ is None:
        return
    if isinstance(input_, str):
        messages.append({"role": "user", "content": input_})
        return
    if not isinstance(input_, list):
        return
    for item in input_:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "message")
        if item_type == "function_call":
            call = {
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                },
            }
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and messages[-1].get("tool_calls")
            ):
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": _function_call_output_text(item.get("output")),
                }
            )
        elif item_type == "reasoning":
            continue
        else:
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"
            message = {"role": role, "content": _responses_message_text(item.get("content"))}
            if role == "system":
                # Chat requires the system prompt first; keep a leading instructions system.
                insert_at = 1 if (messages and messages[0].get("role") == "system") else 0
                messages.insert(insert_at, message)
            else:
                messages.append(message)


def _responses_tools_to_chat(tools: object) -> list[dict]:
    """Convert Responses function tools (flat ``{type, name, description, parameters}``) to
    Chat's nested ``{type: function, function: {...}}`` shape. Non-function tools are skipped."""
    if not isinstance(tools, list):
        return []
    converted: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        nested = tool.get("function")
        if isinstance(nested, dict):
            function = dict(nested)
        else:
            function = {"name": tool.get("name") or ""}
            if tool.get("description") is not None:
                function["description"] = tool["description"]
            if "parameters" in tool:
                function["parameters"] = tool["parameters"]
        converted.append({"type": "function", "function": function})
    return converted


def _responses_tool_choice_to_chat(tool_choice: object) -> object | None:
    """Map a Responses ``tool_choice`` to the Chat form; ``None`` to omit it."""
    if tool_choice in ("auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def responses_to_chat_request(req: dict) -> dict:
    """Translate a codex Responses-API request body into a Chat Completions request body.

    ``instructions`` (codex's system prompt) becomes the leading system message; ``input``
    items become the conversation ``messages``; ``tools`` are reshaped; ``max_output_tokens``
    → ``max_tokens``. Streaming is requested with usage so the final ``response.completed`` can
    carry token counts.
    """
    messages: list[dict] = []
    system_text = req.get("instructions") or req.get("system")
    if isinstance(system_text, str) and system_text:
        messages.append({"role": "system", "content": system_text})
    _append_input_messages(messages, req.get("input"))

    chat: dict = {"messages": messages, "stream": True, "stream_options": {"include_usage": True}}
    if req.get("model"):
        chat["model"] = req["model"]
    tools = _responses_tools_to_chat(req.get("tools"))
    if tools:
        chat["tools"] = tools
        tool_choice = _responses_tool_choice_to_chat(req.get("tool_choice"))
        if tool_choice is not None:
            chat["tool_choice"] = tool_choice
    if isinstance(req.get("max_output_tokens"), int):
        chat["max_tokens"] = req["max_output_tokens"]
    for key in ("temperature", "top_p"):
        if req.get(key) is not None:
            chat[key] = req[key]
    return chat


def _sse_frame(event_type: str, data: dict) -> bytes:
    """One Responses-API SSE frame: an ``event:`` line plus a single-line ``data:`` JSON."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _iter_chat_sse(read) -> object:
    """Yield each upstream Chat Completions SSE ``data:`` payload as a parsed dict.

    ``read`` is a ``response.read``-style callable. Lines are reassembled across chunk
    boundaries; ``data: [DONE]`` ends the stream; unparseable payloads are skipped.
    """
    buffer = b""
    while True:
        chunk = read(4096)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                return
            try:
                yield json.loads(payload)
            except ValueError:
                continue


def translate_chat_stream(read, *, model: str) -> object:
    """Yield Responses-API SSE frames (bytes) translated from an upstream Chat Completions
    SSE stream read via ``read`` (a ``response.read``-style callable).

    Text sequence: ``response.created`` → ``response.output_item.added`` (message) →
    ``response.output_text.delta``* → ``response.output_text.done`` →
    ``response.output_item.done`` → ``response.completed``. Tool calls accumulate from the Chat
    ``delta.tool_calls`` and emit a ``function_call`` item (``added`` →
    ``function_call_arguments.delta`` → ``...done`` → ``output_item.done``) after any message.
    """
    seq = count()
    response_id = "resp_" + uuid.uuid4().hex
    msg_item_id = "msg_" + uuid.uuid4().hex
    yield _sse_frame(
        "response.created",
        {
            "type": "response.created",
            "sequence_number": next(seq),
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": model,
                "output": [],
            },
        },
    )

    text_parts: list[str] = []
    message_started = False
    tool_calls: dict[int, dict] = {}
    usage: dict | None = None

    for data in _iter_chat_sse(read):
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        choices = data.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            if not message_started:
                message_started = True
                yield _sse_frame(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "sequence_number": next(seq),
                        "output_index": 0,
                        "item": {
                            "id": msg_item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
            text_parts.append(content)
            yield _sse_frame(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": next(seq),
                    "item_id": msg_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": content,
                },
            )
        for tool_call in delta.get("tool_calls") or []:
            index = tool_call.get("index", 0)
            accumulator = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if tool_call.get("id"):
                accumulator["id"] = tool_call["id"]
            function = tool_call.get("function") or {}
            if function.get("name"):
                accumulator["name"] = function["name"]
            if function.get("arguments"):
                accumulator["arguments"] += function["arguments"]

    output: list[dict] = []
    if message_started:
        full_text = "".join(text_parts)
        yield _sse_frame(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "sequence_number": next(seq),
                "item_id": msg_item_id,
                "output_index": 0,
                "content_index": 0,
                "text": full_text,
            },
        )
        message_item = {
            "id": msg_item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text}],
        }
        yield _sse_frame(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "sequence_number": next(seq),
                "output_index": 0,
                "item": message_item,
            },
        )
        output.append(message_item)

    base_index = 1 if message_started else 0
    for relative_index, key in enumerate(sorted(tool_calls)):
        accumulator = tool_calls[key]
        output_index = base_index + relative_index
        call_id = accumulator["id"] or ("call_" + uuid.uuid4().hex)
        fc_item_id = "fc_" + uuid.uuid4().hex
        arguments = accumulator["arguments"]
        yield _sse_frame(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "sequence_number": next(seq),
                "output_index": output_index,
                "item": {
                    "id": fc_item_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": accumulator["name"],
                    "arguments": "",
                },
            },
        )
        if arguments:
            yield _sse_frame(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": next(seq),
                    "item_id": fc_item_id,
                    "output_index": output_index,
                    "delta": arguments,
                },
            )
        yield _sse_frame(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "sequence_number": next(seq),
                "item_id": fc_item_id,
                "output_index": output_index,
                "arguments": arguments,
            },
        )
        function_item = {
            "id": fc_item_id,
            "type": "function_call",
            "status": "completed",
            "call_id": call_id,
            "name": accumulator["name"],
            "arguments": arguments,
        }
        yield _sse_frame(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "sequence_number": next(seq),
                "output_index": output_index,
                "item": function_item,
            },
        )
        output.append(function_item)

    completed: dict = {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output,
    }
    if usage:
        completed["usage"] = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    yield _sse_frame(
        "response.completed",
        {"type": "response.completed", "sequence_number": next(seq), "response": completed},
    )


class _ResponsesToChatHandler(BaseHTTPRequestHandler):
    """Translate codex's ``POST /responses`` into an upstream ``/chat/completions`` call and
    stream the Responses-API SSE back. Subclassed per proxy instance to bind ``upstream``."""

    upstream = ""
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        self._proxy()

    def do_GET(self) -> None:  # noqa: N802
        # codex preflights GET /models for metadata enrichment; the upstream's OpenAI-shaped
        # list (`{data:[…]}`) does not match codex's expected `{models:[…]}`, so forwarding it
        # makes codex log a decode error. An empty, valid `{models: []}` answers the probe
        # cleanly — codex falls back to per-model defaults (the metadata warning is harmless).
        # Translating real metadata would need codex's full model schema; out of MVP scope.
        self.close_connection = True
        read_http_body(self.rfile, self.headers)
        payload = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (OSError, http.client.HTTPException):
            self.close_connection = True

    def _proxy(self) -> None:
        # One request per connection: the client then keeps no idle socket to reset later.
        self.close_connection = True

        raw = read_http_body(self.rfile, self.headers)
        try:
            req = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            req = {}
        if not isinstance(req, dict):
            req = {}
        model = str(req.get("model") or "")
        chat_body = json.dumps(responses_to_chat_request(req)).encode()

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() not in ("host", "content-length")
        }
        headers["Content-Length"] = str(len(chat_body))
        headers["Accept"] = "text/event-stream"

        upstream = urlsplit(self.upstream)
        path = upstream.path.rstrip("/") + "/chat/completions"
        connection = (
            http.client.HTTPSConnection(upstream.hostname, upstream.port, timeout=300)
            if upstream.scheme == "https"
            else http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=300)
        )
        try:
            connection.request("POST", path, body=chat_body, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            self._send_failed(502, f"upstream error: {exc}")
            return

        if response.status != 200:
            # Surface the upstream error body verbatim so codex shows the real cause.
            body = response.read()
            connection.close()
            self.send_response(response.status)
            content_type = response.getheader("Content-Type") or "application/json"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for frame in translate_chat_stream(response.read, model=model):
                self.wfile.write(frame)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # Downstream client gone or upstream dropped mid-stream — routine for a proxy.
            self.close_connection = True
        finally:
            connection.close()

    def _send_failed(self, status: int, message: str) -> None:
        """Emit a single Responses ``response.failed`` SSE frame for an upstream failure."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        frame = _sse_frame(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "id": "resp_" + uuid.uuid4().hex,
                    "status": "failed",
                    "error": {"code": str(status), "message": f"agedum codex proxy: {message}"},
                },
            },
        )
        try:
            self.wfile.write(frame)
            self.wfile.flush()
        except (OSError, http.client.HTTPException):
            self.close_connection = True

    def log_message(self, *args) -> None:  # silence the default stderr access log
        pass


class ResponsesToChatProxy:
    """A running localhost proxy that translates codex Responses-API requests into Chat
    Completions calls against ``upstream`` (and the streamed response back).

    Use as a context manager; :attr:`base_url` is the address to set as the codex provider's
    ``base_url`` while the ``with`` block is open (codex appends ``/responses``).
    """

    def __init__(self, upstream: str) -> None:
        handler = type("_BoundResponsesHandler", (_ResponsesToChatHandler,), {"upstream": upstream})
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>`` — the ephemeral address the proxy bound to."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> ResponsesToChatProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
