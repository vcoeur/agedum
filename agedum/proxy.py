"""Localhost reverse proxies that adapt Claude Code's Anthropic Messages traffic to a
compat upstream.

Two proxies share one transport skeleton (:class:`_BaseProxyHandler` +
:class:`_QuietThreadingHTTPServer`):

* :class:`FoldProxy` — for a *strict Anthropic-compat* upstream (notably DeepSeek's
  ``/anthropic``) that rejects a ``system`` role inside the ``messages`` array. Claude Code
  emits hook ``additionalContext`` (e.g. the SessionStart memory reminder) as a
  ``system``-role message in ``messages`` alongside the genuine top-level ``system`` prompt;
  the real Anthropic API and lenient endpoints tolerate it, strict ones return
  ``400 unknown variant 'system'``. FoldProxy folds those entries into the top-level
  ``system`` field (always API-valid) and relays the response back byte-for-byte.

* :class:`TranslateProxy` — for an *OpenAI-only* upstream (one that exposes
  ``/v1/chat/completions`` but no working Anthropic ``/v1/messages`` — e.g. OpenCode Go,
  whose Anthropic surface mistranslates tool definitions). It translates the request
  Anthropic→OpenAI, rewrites the path to ``/v1/chat/completions``, swaps ``x-api-key`` for a
  ``Bearer`` token, and re-serialises the (streamed) OpenAI response back to Anthropic SSE.
  Scoped to the subset Claude Code actually emits — not the full Anthropic API.

In both cases the launcher points the child's ``ANTHROPIC_BASE_URL`` at the proxy and tears
it down when the child exits.

Transport stance: a reverse proxy sees peers hang up constantly — an idle socket reaped by
the client, a generation interrupted mid-stream, a connection reset before the request line
is even read. Those are routine, not errors. :class:`_QuietThreadingHTTPServer` absorbs the
connection-level exception families in one place so they never surface as stderr tracebacks,
the proxy serves **one request per connection** (``Connection: close``, so the client keeps
no idle sockets to reset later), and the upstream hop uses :mod:`http.client` directly, whose
single exception family makes upstream failures uniform to handle.
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


# ---------------------------------------------------------------------------
# Anthropic Messages -> OpenAI Chat Completions (request)
# ---------------------------------------------------------------------------

# OpenAI ``finish_reason`` -> Anthropic ``stop_reason``.
_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def _system_to_text(system: object) -> str:
    """Join the Anthropic top-level ``system`` (string or text blocks) into one string."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [
            block.get("text") or ""
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        parts += [block for block in system if isinstance(block, str)]
        return "\n\n".join(part for part in parts if part)
    return ""


def _strip_formats(schema: object) -> object:
    """Recursively drop JSON-Schema ``format`` keywords from a tool's ``input_schema``.

    Some OpenAI-compatible backends reject formats they don't recognise (e.g. ``uri``)
    with a 400; ``format`` is advisory, so stripping it is safe and keeps the schema usable.
    """
    if isinstance(schema, dict):
        return {key: _strip_formats(value) for key, value in schema.items() if key != "format"}
    if isinstance(schema, list):
        return [_strip_formats(item) for item in schema]
    return schema


def _tool_result_text(block: dict) -> str:
    """Flatten an Anthropic ``tool_result`` block's content to a string for OpenAI.

    Array content is joined; ``is_error`` has no OpenAI equivalent, so it is folded into
    the text as a marker rather than dropped silently.
    """
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(item.get("text") or "")
            elif isinstance(item, str):
                chunks.append(item)
        text = "\n".join(chunks)
    else:
        text = ""
    if block.get("is_error"):
        return f"[tool error] {text}" if text else "[tool error]"
    return text


def _convert_assistant_content(blocks: list) -> list[dict]:
    """An assistant message's block list -> one OpenAI assistant message.

    Text blocks concatenate into ``content``; ``tool_use`` blocks become ``tool_calls``
    with the object ``input`` serialised to a JSON ``arguments`` string.
    """
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}, separators=(",", ":")),
                    },
                }
            )
    message: dict = {"role": "assistant"}
    text = "".join(text_parts)
    if tool_calls:
        message["content"] = text or None
        message["tool_calls"] = tool_calls
    else:
        message["content"] = text
    return [message]


def _convert_user_content(blocks: list) -> list[dict]:
    """A user message's block list -> OpenAI messages.

    ``tool_result`` blocks become separate ``role:"tool"`` messages (emitted first, so they
    follow the assistant ``tool_calls`` turn); text/image blocks become a ``role:"user"``
    message — a lone text block is unwrapped to a string, otherwise content-parts (images as
    base64 data URLs) are kept.
    """
    tool_messages: list[dict] = []
    parts: list[dict] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append({"type": "text", "text": block.get("text") or ""})
        elif block_type == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                url = f"data:{source.get('media_type', '')};base64,{source.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif block_type == "tool_result":
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _tool_result_text(block),
                }
            )
    messages = list(tool_messages)
    if len(parts) == 1 and parts[0].get("type") == "text":
        messages.append({"role": "user", "content": parts[0]["text"]})
    elif parts:
        messages.append({"role": "user", "content": parts})
    return messages


def _convert_message(message: dict) -> list[dict]:
    """One Anthropic message -> one or more OpenAI messages (tool_results fan out)."""
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        return []
    if role == "assistant":
        return _convert_assistant_content(content)
    return _convert_user_content(content)


def _convert_tool(tool: dict) -> dict:
    """An Anthropic tool def -> an OpenAI function tool (``input_schema`` -> ``parameters``)."""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name"),
            "description": tool.get("description") or "",
            "parameters": _strip_formats(tool.get("input_schema") or {}),
        },
    }


def _convert_tool_choice(choice: object) -> object | None:
    """Anthropic ``tool_choice`` -> OpenAI ``tool_choice`` (``any`` -> ``required``)."""
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice.get("name")}}
    return None


def anthropic_to_openai_request(
    body: dict, *, model: str | None = None, reasoning_effort: str | None = None
) -> dict:
    """Translate a Claude Code Anthropic Messages request into an OpenAI Chat Completions one.

    :param body: the parsed Anthropic ``/v1/messages`` request body.
    :param model: when set, overrides the body's model (the config's upstream model id).
    :param reasoning_effort: when set, injected as OpenAI ``reasoning_effort``.
    :returns: an OpenAI ``/v1/chat/completions`` request body.

    Scoped to what Claude Code emits: ``system`` (folded to a leading system message),
    text/image/tool_use/tool_result content, ``tools``/``tool_choice``, and the common
    sampling params. ``thinking``/``metadata``/``cache_control`` have no OpenAI equivalent
    and are dropped.
    """
    messages: list[dict] = []
    system_text = _system_to_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for message in body.get("messages") or []:
        messages.extend(_convert_message(message))

    result: dict = {"model": model or body.get("model") or "", "messages": messages}

    for source_key, dest_key in (
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
    ):
        value = body.get(source_key)
        if value is not None:
            result[dest_key] = value
    stop = body.get("stop_sequences")
    if stop:
        result["stop"] = stop

    tools = body.get("tools")
    if tools:
        result["tools"] = [_convert_tool(tool) for tool in tools if isinstance(tool, dict)]
        tool_choice = _convert_tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            result["tool_choice"] = tool_choice

    if reasoning_effort:
        result["reasoning_effort"] = reasoning_effort

    stream = bool(body.get("stream"))
    result["stream"] = stream
    if stream:
        # Real token usage is only reported on the final chunk when this is requested.
        result["stream_options"] = {"include_usage": True}
    return result


# ---------------------------------------------------------------------------
# OpenAI Chat Completions -> Anthropic Messages (non-streaming response)
# ---------------------------------------------------------------------------


def openai_to_anthropic_response(data: dict, *, model: str | None = None) -> dict:
    """Translate a non-streaming OpenAI Chat Completions response into an Anthropic one.

    ``tool_calls`` become ``tool_use`` blocks (the JSON ``arguments`` string parsed back to
    an ``input`` object); ``finish_reason`` maps to ``stop_reason``; usage maps to Anthropic
    token fields (``cache_creation_input_tokens`` has no OpenAI source, so it is omitted).
    """
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (ValueError, TypeError):
            arguments = {}
        content.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id"),
                "name": function.get("name"),
                "input": arguments,
            }
        )

    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "id": data.get("id") or "msg_agedum",
        "type": "message",
        "role": "assistant",
        "model": data.get("model") or model or "",
        "content": content,
        "stop_reason": _STOP_REASON.get(choice.get("finish_reason") or "stop", "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
            "cache_read_input_tokens": details.get("cached_tokens") or 0,
        },
    }


# ---------------------------------------------------------------------------
# OpenAI Chat Completions SSE -> Anthropic Messages SSE (streaming response)
# ---------------------------------------------------------------------------


class OpenAIToAnthropicStream:
    """Re-serialise an OpenAI Chat Completions SSE stream into Anthropic Messages events.

    OpenAI streams a flat sequence of ``chat.completion.chunk`` deltas; Anthropic expects a
    structured event sequence (``message_start`` -> per-block
    ``content_block_start``/``content_block_delta``/``content_block_stop`` ->
    ``message_delta`` -> ``message_stop``). This holds the state needed to bridge the two:
    which content blocks are open and at what index, tool-call identity that may arrive
    before its arguments, and the running usage.

    Feed each parsed OpenAI chunk to :meth:`feed`; call :meth:`finish` once at stream end.
    Both return a list of encoded Anthropic SSE event ``bytes`` ready to write downstream.
    """

    def __init__(self, model: str = "") -> None:
        self.model = model
        self.message_id = "msg_agedum"
        self.started = False
        self.finished = False
        self.text_index: int | None = None
        self.next_index = 0
        self.open_indices: set[int] = set()
        # OpenAI tool_calls[].index -> {anthropic_index, started, id, name, buffer}
        self.tool_blocks: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self._output_chars = 0

    @staticmethod
    def _event(event_type: str, data: dict) -> bytes:
        return f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    def _ensure_started(self, chunk: dict | None = None) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        if chunk:
            self.message_id = chunk.get("id") or self.message_id
            self.model = chunk.get("model") or self.model
        return [
            self._event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        # input_tokens is unknown until the final usage chunk; output_tokens
                        # is the synthetic seed the Anthropic envelope requires.
                        "usage": {"input_tokens": 0, "output_tokens": 1},
                    },
                },
            )
        ]

    def _absorb_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        self.input_tokens = usage.get("prompt_tokens") or self.input_tokens
        self.output_tokens = usage.get("completion_tokens") or self.output_tokens
        details = usage.get("prompt_tokens_details") or {}
        self.cache_read = details.get("cached_tokens") or self.cache_read

    def _open_text_block(self) -> list[bytes]:
        if self.text_index is not None:
            return []
        self.text_index = self.next_index
        self.next_index += 1
        self.open_indices.add(self.text_index)
        return [
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.text_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        ]

    def _content_block_stop(self, index: int) -> bytes:
        return self._event("content_block_stop", {"type": "content_block_stop", "index": index})

    def _input_json_delta(self, index: int, partial_json: str) -> bytes:
        return self._event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        )

    def _feed_tool_call(self, tool_call: dict) -> list[bytes]:
        events: list[bytes] = []
        openai_index = tool_call.get("index") or 0
        block = self.tool_blocks.setdefault(
            openai_index,
            {"anthropic_index": None, "started": False, "id": None, "name": None, "buffer": ""},
        )
        if tool_call.get("id"):
            block["id"] = tool_call["id"]
        function = tool_call.get("function") or {}
        if function.get("name"):
            block["name"] = function["name"]
        arguments = function.get("arguments") or ""

        if not block["started"] and block["id"] and block["name"]:
            # A tool block follows the text: close the text block before opening it.
            if self.text_index is not None and self.text_index in self.open_indices:
                self.open_indices.discard(self.text_index)
                events.append(self._content_block_stop(self.text_index))
            block["anthropic_index"] = self.next_index
            self.next_index += 1
            block["started"] = True
            self.open_indices.add(block["anthropic_index"])
            events.append(
                self._event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block["anthropic_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": {},
                        },
                    },
                )
            )
            if block["buffer"]:
                events.append(self._input_json_delta(block["anthropic_index"], block["buffer"]))
                self._output_chars += len(block["buffer"])
                block["buffer"] = ""

        if arguments:
            if block["started"]:
                events.append(self._input_json_delta(block["anthropic_index"], arguments))
                self._output_chars += len(arguments)
            else:
                # Arguments can arrive before id/name; hold them until the block is open.
                block["buffer"] += arguments
        return events

    def feed(self, chunk: dict) -> list[bytes]:
        """Translate one OpenAI chunk; returns the Anthropic SSE events it produces."""
        events = self._ensure_started(chunk)
        choices = chunk.get("choices") or []
        if not choices:
            # A trailing usage-only chunk (``stream_options.include_usage``).
            self._absorb_usage(chunk.get("usage"))
            return events
        choice = choices[0]
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if isinstance(content, str) and content:
            events.extend(self._open_text_block())
            events.append(
                self._event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.text_index,
                        "delta": {"type": "text_delta", "text": content},
                    },
                )
            )
            self._output_chars += len(content)

        for tool_call in delta.get("tool_calls") or []:
            events.extend(self._feed_tool_call(tool_call))

        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        self._absorb_usage(chunk.get("usage"))
        return events

    def finish(self) -> list[bytes]:
        """Emit the closing events (block stops, ``message_delta``, ``message_stop``)."""
        if self.finished:
            return []
        self.finished = True
        events = self._ensure_started()
        for index in sorted(self.open_indices):
            events.append(self._content_block_stop(index))
        self.open_indices.clear()
        output_tokens = self.output_tokens or max(1, self._output_chars // 4)
        usage: dict = {"output_tokens": output_tokens}
        if self.input_tokens:
            usage["input_tokens"] = self.input_tokens
        if self.cache_read:
            usage["cache_read_input_tokens"] = self.cache_read
        events.append(
            self._event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _STOP_REASON.get(self.finish_reason or "stop", "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": usage,
                },
            )
        )
        events.append(self._event("message_stop", {"type": "message_stop"}))
        return events


def _anthropic_error_type(status: int) -> str:
    """Map an HTTP status to the Anthropic error ``type`` string."""
    mapping = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
    }
    if status in mapping:
        return mapping[status]
    return "api_error" if status >= 500 else "invalid_request_error"


# ---------------------------------------------------------------------------
# Transport — shared server + per-connection handler base
# ---------------------------------------------------------------------------


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


class _BaseProxyHandler(BaseHTTPRequestHandler):
    """Per-connection forward mechanics shared by the fold and translate proxies.

    The transport-invariant parts live here — verb dispatch, body framing (Content-Length
    and chunked), header hygiene, the :mod:`http.client` upstream hop with its timeout
    backstop, and silenced logging. Subclasses supply the transform seams:
    :meth:`transform_request` (Anthropic body in -> upstream body/path/headers out) and
    :meth:`relay_response` (upstream response -> downstream bytes), plus an optional
    :meth:`short_circuit` for requests answered locally. ``upstream`` is bound per proxy
    instance so no globals are needed.
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

    # --- transform seams (overridden by subclasses) -----------------------

    def transform_request(
        self, raw: bytes, path: str, headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        """Return the ``(body, path, headers)`` to forward upstream. Default: identity."""
        return raw, path, headers

    def relay_response(self, response: http.client.HTTPResponse) -> None:
        """Send the upstream response downstream. Default: byte-for-byte passthrough."""
        self._relay_passthrough(response)

    def short_circuit(self, path: str, raw: bytes) -> dict | None:
        """A request answered locally without an upstream hop, or ``None`` to forward."""
        return None

    # --- forward mechanics -------------------------------------------------

    def _proxy(self) -> None:
        # One request per connection: the client then keeps no idle socket to reset later.
        self.close_connection = True

        raw = self._read_body()
        local = self.short_circuit(self.path, raw)
        if local is not None:
            self._send_json(200, local)
            return

        body, path, headers = self.transform_request(raw, self.path, dict(self.headers.items()))
        headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() not in ("host", "content-length")
        }
        headers["Content-Length"] = str(len(body))

        upstream = urlsplit(self.upstream)
        full_path = upstream.path.rstrip("/") + path
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
            connection.request(self.command, full_path, body=body, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            # Upstream unreachable, or dropped mid-flight (RemoteDisconnected /
            # IncompleteRead). http.client raises one predictable family for both, so a
            # single clause covers what urllib split across URLError and bare
            # ConnectionError. A live client still gets a clean 502 body.
            connection.close()
            self._send_error(502, f"agedum proxy: upstream error: {exc}")
            return
        try:
            self.relay_response(response)
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

    def _relay_passthrough(self, response: http.client.HTTPResponse) -> None:
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

    def _send_json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(
            status, {"type": "error", "error": {"type": "api_error", "message": message}}
        )

    def log_message(self, *args) -> None:  # silence the default stderr access log
        pass


class _FoldHandler(_BaseProxyHandler):
    """Fold ``system``-role messages into the top-level ``system`` field, then relay verbatim."""

    def transform_request(
        self, raw: bytes, path: str, headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        return self._maybe_fold(raw), path, headers

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


class _TranslateHandler(_BaseProxyHandler):
    """Translate Anthropic Messages <-> OpenAI Chat Completions across the upstream hop.

    Request: Anthropic body -> OpenAI body, path ``/v1/messages`` -> ``/v1/chat/completions``,
    ``x-api-key`` -> ``Authorization: Bearer``. Response: a streamed OpenAI SSE response is
    re-serialised to Anthropic SSE (:class:`OpenAIToAnthropicStream`); a non-streaming JSON
    response is translated whole; an upstream error becomes an Anthropic error envelope.
    ``/v1/messages/count_tokens`` is answered locally with a chars/4 heuristic.
    """

    model = ""
    reasoning_effort = ""

    def short_circuit(self, path: str, raw: bytes) -> dict | None:
        if not path.rstrip("/").endswith("/count_tokens"):
            return None
        # No OpenAI token-count endpoint exists; a cheap chars/4 estimate keeps Claude Code's
        # context budgeting working. Imprecise by design — documented as a v1 limitation.
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return {"input_tokens": 0}
        if not isinstance(body, dict):
            return {"input_tokens": 0}
        chars = len(_system_to_text(body.get("system")))
        for message in body.get("messages") or []:
            if isinstance(message, dict):
                chars += len(_extract_text(message.get("content")))
        return {"input_tokens": max(1, chars // 4)}

    def transform_request(
        self, raw: bytes, path: str, headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        new_path = "/v1/chat/completions" if path.rstrip("/").endswith("/v1/messages") else path
        return self._translate_body(raw), new_path, self._translate_headers(headers)

    def _translate_body(self, raw: bytes) -> bytes:
        if not raw:
            return raw
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return raw
        if not isinstance(body, dict):
            return raw
        translated = anthropic_to_openai_request(
            body, model=self.model or None, reasoning_effort=self.reasoning_effort or None
        )
        return json.dumps(translated, separators=(",", ":")).encode("utf-8")

    def _translate_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Drop Anthropic-only auth/version headers; carry the key as a Bearer token."""
        out: dict[str, str] = {}
        token = ""
        for name, value in headers.items():
            lowered = name.lower()
            if lowered == "x-api-key":
                token = value
            elif lowered == "authorization":
                token = value[7:].strip() if value.lower().startswith("bearer ") else value
            elif lowered in ("anthropic-version", "anthropic-beta"):
                continue
            else:
                out[name] = value
        if token:
            out["Authorization"] = f"Bearer {token}"
        out["Content-Type"] = "application/json"
        return out

    def relay_response(self, response: http.client.HTTPResponse) -> None:
        if response.status >= 400:
            self._relay_error(response)
            return
        content_type = (response.getheader("Content-Type") or "").lower()
        if "text/event-stream" in content_type:
            self._relay_stream(response)
        else:
            self._relay_json(response)

    def _relay_json(self, response: http.client.HTTPResponse) -> None:
        raw = response.read()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._send_error(502, "agedum translate-proxy: malformed upstream JSON")
            return
        self._send_json(200, openai_to_anthropic_response(data, model=self.model or None))

    def _relay_error(self, response: http.client.HTTPResponse) -> None:
        """Translate an OpenAI error body to the Anthropic envelope, preserving the status."""
        raw = response.read()
        message = raw.decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error)
            elif isinstance(error, str):
                message = error
            elif data.get("message"):
                message = data["message"]
        self._send_json(
            response.status,
            {
                "type": "error",
                "error": {"type": _anthropic_error_type(response.status), "message": message},
            },
        )

    def _relay_stream(self, response: http.client.HTTPResponse) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        stream = OpenAIToAnthropicStream(self.model or "")
        while True:
            line = response.readline()
            if not line:
                break
            events = _feed_sse_line(stream, line)
            for event in events:
                self.wfile.write(event)
            if events:
                self.wfile.flush()
        for event in stream.finish():
            self.wfile.write(event)
        self.wfile.flush()


def _feed_sse_line(stream: OpenAIToAnthropicStream, line: bytes) -> list[bytes]:
    """Parse one OpenAI SSE ``data:`` line and feed it to ``stream`` (blank/``[DONE]`` skipped)."""
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("data:"):
        return []
    payload = text[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return []
    try:
        chunk = json.loads(payload)
    except ValueError:
        # A malformed line: skip it rather than crash the relay.
        return []
    return stream.feed(chunk)


# ---------------------------------------------------------------------------
# Proxy lifecycle
# ---------------------------------------------------------------------------


class _LocalProxy:
    """A localhost proxy server bound to an ephemeral port, run as a context manager."""

    def __init__(self, handler_cls: type[_BaseProxyHandler]) -> None:
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>`` — the ephemeral address the proxy bound to."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _LocalProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class FoldProxy(_LocalProxy):
    """A running localhost proxy that folds system-role messages toward ``upstream``.

    Use as a context manager; :attr:`base_url` is the address to point
    ``ANTHROPIC_BASE_URL`` at while the ``with`` block is open.
    """

    def __init__(self, upstream: str) -> None:
        super().__init__(type("_BoundFoldHandler", (_FoldHandler,), {"upstream": upstream}))


class TranslateProxy(_LocalProxy):
    """A localhost proxy that translates Anthropic<->OpenAI toward an OpenAI ``upstream``.

    Use as a context manager; :attr:`base_url` is the address to point
    ``ANTHROPIC_BASE_URL`` at while the ``with`` block is open. ``model`` overrides the
    request model with the upstream's id; ``reasoning_effort`` is injected when set.
    """

    def __init__(self, upstream: str, *, model: str = "", reasoning_effort: str = "") -> None:
        super().__init__(
            type(
                "_BoundTranslateHandler",
                (_TranslateHandler,),
                {
                    "upstream": upstream,
                    "model": model or "",
                    "reasoning_effort": reasoning_effort or "",
                },
            )
        )
