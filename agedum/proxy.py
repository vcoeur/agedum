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


def _parse_json_dict(raw: bytes) -> dict | None:
    """Parse a request body to a JSON object, or ``None`` if empty / not-JSON / not-a-dict."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


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


def _stop_reason(finish_reason: str | None) -> str:
    """Map an OpenAI ``finish_reason`` to an Anthropic ``stop_reason`` (default ``end_turn``)."""
    return _STOP_REASON.get(finish_reason or "stop", "end_turn")


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
    body: dict,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    prompt_cache_key: str | None = None,
    thinking_mode: str = "",
) -> dict:
    """Translate a Claude Code Anthropic Messages request into an OpenAI Chat Completions one.

    :param body: the parsed Anthropic ``/v1/messages`` request body.
    :param model: when set, overrides the body's model (the config's upstream model id).
    :param reasoning_effort: when set, injected as OpenAI ``reasoning_effort``.
    :param prompt_cache_key: when set, injected as a top-level ``prompt_cache_key`` — a
        prefix-cache routing hint honoured by Moonshot/Kimi and other OpenAI-compatible backends.
    :param thinking_mode: ``"toggle"`` maps Anthropic ``thinking`` to Moonshot's on/off
        ``thinking: {"type": "enabled"|"disabled"}`` param; empty leaves ``thinking`` dropped.
    :returns: an OpenAI ``/v1/chat/completions`` request body.

    Scoped to what Claude Code emits: ``system`` (folded to a leading system message),
    text/image/tool_use/tool_result content, ``tools``/``tool_choice``, and the common
    sampling params. ``metadata`` and per-block ``cache_control`` have no OpenAI equivalent and
    are dropped; ``thinking`` is dropped unless ``thinking_mode`` maps it (above), and
    session-level prefix caching is hinted via ``prompt_cache_key`` when supplied.
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

    if prompt_cache_key:
        # A prefix-cache routing hint: reusing one value across a conversation raises the
        # cache-hit rate on Moonshot/Kimi. Purely an optimization — an absent or non-matching
        # key only lowers the hit rate, it never changes the result or errors.
        result["prompt_cache_key"] = prompt_cache_key

    if thinking_mode == "toggle":
        # Moonshot exposes an on/off reasoning toggle (`thinking: {"type": ...}`); pass an
        # explicit enabled/disabled straight through. Absent thinking (or a `budget_tokens`,
        # which has no equivalent) is left to the model default. Never enable this mode for an
        # always-think model (e.g. kimi-k2.7-code) — it errors on "disabled".
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "disabled"):
            result["thinking"] = {"type": thinking["type"]}

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
    choice = (data.get("choices") or [{}])[0] or {}
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
        "stop_reason": _stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
            # Standard OpenAI nests it under prompt_tokens_details; some backends (Moonshot)
            # may report it flat on usage — accept either.
            "cache_read_input_tokens": details.get("cached_tokens")
            or usage.get("cached_tokens")
            or 0,
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
        self.next_index = 0
        # Anthropic requires content blocks to be opened and closed strictly in sequence —
        # one open at a time. Track the single currently-open block; a new block closes it.
        self.current_index: int | None = None
        self.current_kind: str | None = None  # "text" | "tool"
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
        self.cache_read = (
            details.get("cached_tokens") or usage.get("cached_tokens") or self.cache_read
        )

    def _close_current(self) -> list[bytes]:
        """Stop the currently-open content block, if any (enforces one-open-at-a-time)."""
        if self.current_index is None:
            return []
        event = self._content_block_stop(self.current_index)
        self.current_index = None
        self.current_kind = None
        return [event]

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
        # `index` keys the tool call across delta fragments; it is always present in the
        # OpenAI streaming schema. Use .get(..., 0) (not `or 0`) so a real index 0 is kept.
        openai_index = tool_call.get("index", 0)
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
            # Close whatever block is open (text, or a prior tool) before opening this one —
            # the target backends stream each tool call's arguments contiguously, so the
            # previous block's deltas have all arrived by now.
            events.extend(self._close_current())
            block["anthropic_index"] = self.next_index
            self.next_index += 1
            block["started"] = True
            self.current_index = block["anthropic_index"]
            self.current_kind = "tool"
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
        choice = choices[0] or {}
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if isinstance(content, str) and content:
            if self.current_kind != "text":
                # Open a fresh text block (closing any open tool block first), so text that
                # resumes after a tool call lands in its own block rather than a stopped one.
                events.extend(self._close_current())
                self.current_index = self.next_index
                self.next_index += 1
                self.current_kind = "text"
                events.append(
                    self._event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": self.current_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
            events.append(
                self._event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.current_index,
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
        events.extend(self._close_current())
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
                        "stop_reason": _stop_reason(self.finish_reason),
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
        parsed = _parse_json_dict(raw)
        if parsed is None:
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
    prompt_cache_key = ""
    thinking_mode = ""
    api_key = ""

    def short_circuit(self, path: str, raw: bytes) -> dict | None:
        if not path.split("?", 1)[0].rstrip("/").endswith("/count_tokens"):
            return None
        # No OpenAI token-count endpoint exists; a cheap chars/4 estimate keeps Claude Code's
        # context budgeting working. Imprecise by design — documented as a v1 limitation.
        body = _parse_json_dict(raw) or {}
        chars = len(_system_to_text(body.get("system")))
        for message in body.get("messages") or []:
            if isinstance(message, dict):
                chars += len(_extract_text(message.get("content")))
        return {"input_tokens": max(1, chars // 4)}

    def transform_request(
        self, raw: bytes, path: str, headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        # Claude Code appends a query string (e.g. `?beta=true`) to /v1/messages. Match on the
        # path component only, and drop the query — it is Anthropic-specific and meaningless to
        # the OpenAI endpoint. (A non-/v1/messages path is forwarded verbatim, defensively.)
        path_only = path.split("?", 1)[0]
        new_path = (
            "/v1/chat/completions" if path_only.rstrip("/").endswith("/v1/messages") else path
        )
        return self._translate_body(raw), new_path, self._translate_headers(headers)

    def _translate_body(self, raw: bytes) -> bytes:
        body = _parse_json_dict(raw)
        if body is None:
            return raw
        translated = anthropic_to_openai_request(
            body,
            model=self.model or None,
            reasoning_effort=self.reasoning_effort or None,
            prompt_cache_key=self.prompt_cache_key or None,
            thinking_mode=self.thinking_mode or "",
        )
        return json.dumps(translated, separators=(",", ":")).encode("utf-8")

    def _translate_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Drop Anthropic-only auth/version headers; carry the resolved key as a Bearer token."""
        out: dict[str, str] = {}
        incoming = ""
        for name, value in headers.items():
            lowered = name.lower()
            if lowered == "x-api-key":
                incoming = value or incoming
            elif lowered == "authorization":
                # Only a fallback (see below); never let it override an x-api-key.
                if not incoming:
                    incoming = value[7:].strip() if value.lower().startswith("bearer ") else value
            elif lowered in ("anthropic-version", "anthropic-beta"):
                continue
            elif lowered == "accept-encoding":
                # Unlike the fold proxy (which relays bytes 1:1), we re-read and re-parse the
                # response body — so force identity encoding rather than risk a gzip body the
                # JSON/SSE parsers would choke on. Claude Code's client advertises gzip.
                continue
            else:
                out[name] = value
        # Prefer the key agedum resolved from the config. Claude Code can send BOTH an
        # x-api-key and a stale Authorization (a cached OAuth token in ~/.claude); relaying
        # whichever it sent last would forward the wrong one and 401. The config key is
        # authoritative; the incoming header is only a fallback when no key was configured.
        token = self.api_key or incoming
        if token:
            out["Authorization"] = f"Bearer {token}"
        out["Content-Type"] = "application/json"
        out["Accept-Encoding"] = "identity"
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
    request model with the upstream's id; ``reasoning_effort`` is injected when set;
    ``api_key`` is the resolved upstream key, sent as ``Authorization: Bearer`` (authoritative
    over whatever auth headers the client sends). ``prompt_cache_key`` is injected as a
    prefix-cache routing hint; ``thinking_mode`` maps Anthropic ``thinking`` on/off (see
    :func:`anthropic_to_openai_request`).
    """

    def __init__(
        self,
        upstream: str,
        *,
        model: str = "",
        reasoning_effort: str = "",
        api_key: str = "",
        prompt_cache_key: str = "",
        thinking_mode: str = "",
    ) -> None:
        super().__init__(
            type(
                "_BoundTranslateHandler",
                (_TranslateHandler,),
                {
                    "upstream": upstream,
                    "model": model or "",
                    "reasoning_effort": reasoning_effort or "",
                    "api_key": api_key or "",
                    "prompt_cache_key": prompt_cache_key or "",
                    "thinking_mode": thinking_mode or "",
                },
            )
        )


# ---------------------------------------------------------------------------
# Responses API <-> Chat Completions translation (codex harness)
# ---------------------------------------------------------------------------
#
# Recent codex speaks ONLY the OpenAI Responses API (wire_api="chat" removed Feb 2026), but
# DeepSeek and most OpenAI-compatible providers serve only Chat Completions. ResponsesToChatProxy
# (a _LocalProxy, like FoldProxy/TranslateProxy) translates codex's POST /responses into a
# /chat/completions call and the streamed Chat deltas back into the Responses SSE event sequence
# codex consumes (text + function calls).


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
    thinking-model reasoning round-trip is a deferred follow-up).
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

    A Chat ``delta.reasoning_content`` (Moonshot/Kimi thinking models stream the chain-of-thought
    on this field) becomes a Responses ``reasoning`` output item at index 0, streamed as
    reasoning-summary events (``output_item.added`` → ``reasoning_summary_part.added`` →
    ``reasoning_summary_text.delta``* → ``...done`` → ``output_item.done``) so codex renders the
    model's thinking. Reasoning always precedes the assistant message in the stream; the message
    (and any tool calls) then follow at the next index.

    Text sequence: ``response.created`` → ``response.output_item.added`` (message) →
    ``response.output_text.delta``* → ``response.output_text.done`` →
    ``response.output_item.done`` → ``response.completed``. Tool calls accumulate from the Chat
    ``delta.tool_calls`` and emit a ``function_call`` item (``added`` →
    ``function_call_arguments.delta`` → ``...done`` → ``output_item.done``) after any message.
    """
    seq = count()
    response_id = "resp_" + uuid.uuid4().hex
    msg_item_id = "msg_" + uuid.uuid4().hex
    reasoning_item_id = "rs_" + uuid.uuid4().hex
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
    reasoning_parts: list[str] = []
    message_started = False
    reasoning_started = False
    reasoning_closed = False
    tool_calls: dict[int, dict] = {}
    usage: dict | None = None
    output: list[dict] = []

    def _open_reasoning():
        # The reasoning item is always the first output item (index 0).
        yield _sse_frame(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "sequence_number": next(seq),
                "output_index": 0,
                "item": {
                    "id": reasoning_item_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            },
        )
        yield _sse_frame(
            "response.reasoning_summary_part.added",
            {
                "type": "response.reasoning_summary_part.added",
                "sequence_number": next(seq),
                "item_id": reasoning_item_id,
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
        )

    def _close_reasoning():
        summary = "".join(reasoning_parts)
        yield _sse_frame(
            "response.reasoning_summary_text.done",
            {
                "type": "response.reasoning_summary_text.done",
                "sequence_number": next(seq),
                "item_id": reasoning_item_id,
                "output_index": 0,
                "summary_index": 0,
                "text": summary,
            },
        )
        yield _sse_frame(
            "response.reasoning_summary_part.done",
            {
                "type": "response.reasoning_summary_part.done",
                "sequence_number": next(seq),
                "item_id": reasoning_item_id,
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": summary},
            },
        )
        reasoning_item = {
            "id": reasoning_item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": summary}],
        }
        yield _sse_frame(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "sequence_number": next(seq),
                "output_index": 0,
                "item": reasoning_item,
            },
        )
        output.append(reasoning_item)

    for data in _iter_chat_sse(read):
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        choices = data.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content")
        # Reasoning precedes the message; ignore any stray reasoning once real output has begun.
        if isinstance(reasoning, str) and reasoning and not message_started and not tool_calls:
            if not reasoning_started:
                reasoning_started = True
                yield from _open_reasoning()
            reasoning_parts.append(reasoning)
            yield _sse_frame(
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "sequence_number": next(seq),
                    "item_id": reasoning_item_id,
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": reasoning,
                },
            )
        content = delta.get("content")
        if isinstance(content, str) and content:
            if reasoning_started and not reasoning_closed:
                reasoning_closed = True
                yield from _close_reasoning()
            if not message_started:
                message_started = True
                yield _sse_frame(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "sequence_number": next(seq),
                        "output_index": 1 if reasoning_started else 0,
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
                    "output_index": 1 if reasoning_started else 0,
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

    # A reasoning-only turn (thinking but no assistant text) still needs its item closed.
    if reasoning_started and not reasoning_closed:
        reasoning_closed = True
        yield from _close_reasoning()

    msg_index = 1 if reasoning_started else 0
    if message_started:
        full_text = "".join(text_parts)
        yield _sse_frame(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "sequence_number": next(seq),
                "item_id": msg_item_id,
                "output_index": msg_index,
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
                "output_index": msg_index,
                "item": message_item,
            },
        )
        output.append(message_item)

    base_index = (1 if reasoning_started else 0) + (1 if message_started else 0)
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


class _ResponsesToChatHandler(_BaseProxyHandler):
    """Translate codex's Responses-API request → ``/chat/completions`` and the streamed Chat
    deltas → Responses-API SSE. The chat-only-provider analog of :class:`_TranslateHandler`,
    built on the shared :class:`_BaseProxyHandler` forward mechanics."""

    # Stashed by transform_request for relay_response (one handler instance per request).
    _model = ""

    def short_circuit(self, path: str, raw: bytes) -> dict | None:
        # codex preflights GET /models for metadata; the upstream's OpenAI-shaped list does
        # not match codex's expected {models:[…]}, so answer locally with an empty valid list —
        # codex falls back to per-model defaults (a benign "metadata not found" warning).
        if self.command == "GET" and path.split("?", 1)[0].rstrip("/").endswith("/models"):
            return {"models": []}
        return None

    def transform_request(
        self, raw: bytes, path: str, headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        req = _parse_json_dict(raw) or {}
        self._model = str(req.get("model") or "")
        body = json.dumps(responses_to_chat_request(req), separators=(",", ":")).encode()
        headers = dict(headers)
        headers["Accept"] = "text/event-stream"
        return body, "/chat/completions", headers

    def relay_response(self, response: http.client.HTTPResponse) -> None:
        if response.status != 200:
            # Surface the upstream error body verbatim so codex shows the real cause.
            self._relay_passthrough(response)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in translate_chat_stream(response.read, model=self._model):
            self.wfile.write(frame)
            self.wfile.flush()


class ResponsesToChatProxy(_LocalProxy):
    """A localhost proxy translating codex's Responses API ⇄ Chat Completions toward a
    Chat-Completions ``upstream`` (DeepSeek etc.).

    Use as a context manager; :attr:`base_url` is the address to set as the codex provider's
    ``base_url`` while the ``with`` block is open (codex appends ``/responses``)."""

    def __init__(self, upstream: str) -> None:
        super().__init__(
            type("_BoundResponsesHandler", (_ResponsesToChatHandler,), {"upstream": upstream})
        )
