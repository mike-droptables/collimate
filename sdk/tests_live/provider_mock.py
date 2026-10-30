# SPDX-License-Identifier: MIT
"""A scripted model provider for driving every REAL harness through a turn
without billing an account: the conformance suite's other end.

One server speaks the four wires the roster bills — Anthropic Messages
(``/v1/messages``), OpenAI Chat Completions (``…/chat/completions``), OpenAI
Responses (``…/responses``) and Gemini (``…/models/<id>:generateContent`` and
``:streamGenerateContent``, ``functionCall`` / ``functionResponse`` parts) —
streamed (SSE) or plain JSON, plus the model lists (``…/models``) harnesses
read at startup. Each turn is scripted by
:attr:`ProviderMock.action`:

* ``("text", reply)`` — answer every request with ``reply``;
* ``("tool", suffix, arguments)`` — answer a request that offers tools by
  calling the first offered tool whose name ends with ``suffix`` (a harness
  namespaces an MCP tool its own way: ``mcp__collimate__list_files``,
  ``collimate_list_files``, ``collimate__list_files``), with ``arguments``.
  A harness that defers MCP tools behind a search gets the search first,
  then the call through its invoker: qwen-code's and hermes's
  ``tool_search`` / ``tool_call``, vibe's ``search_tool_functions`` /
  ``run_typescript`` (``tools.<server>.<tool>({})``); cline's
  ``use_mcp_tool`` names server and tool; codex's Responses ``namespace``
  tools (``mcp__collimate`` → ``list_files``) are matched flattened and
  called with their ``namespace``;
* ``("shell", command)`` — call the harness's own shell tool (whatever it is
  named: :data:`SHELL_TOOLS`) with ``command``, shaped to its schema (an argv
  array where the schema says ``array``, required booleans ``False``);
* ``("shell_all", command)`` — call every offered shell-like tool in turn;
* ``("seq", [action, …])`` — play several of these in order (a bypass
  vector: write a file with one tool, then trigger it with another).

Once a request carries the tool's result, the reply is ``TOOL RESULT: `` plus
that result's text, so the result reaching the model is visible in the
harness's final message. A ``shell`` turn whose request offers no shell tool
(the harness withheld it) answers ``NO-SHELL``. Requests that offer no tools
(titles, summaries, compaction) get a short text reply. Every request is kept
in :attr:`ProviderMock.requests` as ``(wire, body)``.

The mock serves only under ``prefix``, the path of the provider's registry
endpoint (``/api/v1`` for OpenRouter): a request anywhere else is a 404 kept
in :attr:`ProviderMock.misses`, so a harness that builds its provider URL
some other way than from the registry endpoint shows up.

The server binds the Docker bridge gateway, the address a harness container
reaches as ``host.docker.internal`` and the host reaches directly, so one mock
serves the in-process, host and container loci (:meth:`url_for`).
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The names a harness's own shell tool goes by, lowercased, without namespace.
SHELL_TOOLS = ("bash", "shell", "run_shell_command", "execute_command", "developer__shell",
               "shell_command", "terminal", "exec_command", "run_terminal_cmd", "run_command",
               "run_commands")

#: Paths a harness probes outside its provider's API that are not a wrong
#: URL: hermes asks an unknown host whether it is Ollama (``/api/show``, its
#: context-length probe), which it never does against a known provider URL;
#: pi 1.4.0 personalizes its OpenRouter catalog from ``<base>/models/user``
#: with the provider-wide base, which for a Claude model is the bare ``/api``
#: its Anthropic client needs, so that GET misses (OpenRouter answers it 404,
#: checked 2026-10-25, and pi then keeps its catalog unchanged).
_PROBES = ("/api/show", "/api/models/user")

#: The request paths the wires serve, relative to the registry endpoint.
_API_PATHS = ("/messages", "/messages/count_tokens", "/chat/completions", "/responses")


def gateway_host() -> str:
    """The Docker bridge gateway (``host.docker.internal`` from a container)."""
    from _agent_shared.mcp_bridge import _docker_gateway_host
    return _docker_gateway_host()


def _text(content) -> str:
    """The text of a tool result in any of the three wires' shapes."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c)
                       for c in content)
    if isinstance(content, dict):
        return _text(content.get("text", content.get("content", "")))
    return "" if content is None else str(content)


def _tool_name(tool: dict) -> str:
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        return str(tool["function"].get("name", ""))
    return str(tool.get("name", ""))


def _tool_schema(tool: dict) -> dict:
    if isinstance(tool.get("function"), dict):
        return tool["function"].get("parameters") or {}
    return tool.get("input_schema") or tool.get("parameters") or {}


def _bare(name: str) -> str:
    """``mcp__collimate__list_files`` → ``list_files``; ``Bash`` → ``bash``."""
    low = name.lower()
    for sep in ("__", "."):
        if sep in low and not low.startswith("developer__"):
            low = low.rsplit(sep, 1)[-1]
    return low


class ProviderMock:
    def __init__(self, action: tuple = ("text", "ACP-OK"), *, prefix: str = "", host: str = "") -> None:
        self.action = action
        self.prefix = prefix.rstrip("/")
        self.requests: list = []
        self.calls: list = []
        self.misses: list = []
        self._last_results = ""
        self._seq_index = 0
        self._seq_started = False
        self._seq_results: list = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer((host or gateway_host(), 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ── where a harness reaches it ────────────────────────────────────
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url_for(self, locus: str, path: str = "/v1") -> str:
        """The base URL a harness at ``locus`` reaches the mock on."""
        host = "host.docker.internal" if locus == "container" else self._server.server_address[0]
        return f"http://{host}:{self.port}{path}"

    def __enter__(self) -> "ProviderMock":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── the scripted turn, wire-independent ───────────────────────────
    def _pick(self, tools: list) -> "tuple | None":
        """``(name, arguments)`` of the tool this turn calls, or None."""
        kind = self.action[0]
        if kind == "tool":
            suffix, arguments = self.action[1], self.action[2]
            named = [t for t in tools if _tool_name(t).lower().endswith(suffix.lower())]
            # A harness's own tool of the same name (cline's list_files) loses
            # to the MCP server's.
            named.sort(key=lambda t: "collimate" not in _tool_name(t).lower())
            if named:
                return _tool_name(named[0]), dict(arguments)
            offered = {_tool_name(t) for t in tools}
            if {"search_tool_functions", "run_typescript"} <= offered:
                if not any(name == "search_tool_functions" for name, _args in self.calls):
                    return "search_tool_functions", {"mode": "best_match", "query": suffix.replace("_", " ")}
                found = re.findall(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]*" + re.escape(suffix), self._last_results)
                function = found[0] if found else f"collimate.{suffix}"
                return "run_typescript", {"code": "async function main() {\n  return await tools.%s(%s);\n}"
                                          % (function, json.dumps(arguments))}
            if "use_mcp_tool" in offered:
                return "use_mcp_tool", {"server_name": "collimate", "tool_name": suffix,
                                        "arguments": json.dumps(arguments)}
            deferred = self._deferred_name(suffix)
            if deferred and "tool_call" in offered:
                if not any(name == "tool_search" for name, _args in self.calls) and "tool_search" in offered:
                    return "tool_search", {"query": f"select:{deferred}"}
                return "tool_call", {"name": deferred, "arguments": dict(arguments)}
            return None
        if kind == "shell":
            command = self.action[1]
            for tool in tools:
                if _bare(_tool_name(tool)) in SHELL_TOOLS:
                    return _tool_name(tool), self._shell_arguments(_tool_schema(tool), command)
            return None
        return None

    def _deferred_name(self, suffix: str) -> str:
        """A tool name ending with ``suffix`` that the conversation mentions
        (a deferred tool's name in the harness's startup reminder)."""
        with self._lock:
            text = json.dumps(self.requests[-1][1]) if self.requests else ""
        found = sorted(set(re.findall(r"[A-Za-z0-9_.-]*" + re.escape(suffix) + r"\b", text)),
                       key=lambda name: ("collimate" not in name.lower(), len(name)))
        return found[0] if found else ""

    def reached(self, suffix: str) -> bool:
        """Whether a turn called the tool ending with ``suffix``, directly or
        through the harness's invoker."""
        for name, args in self.calls:
            if name == "run_typescript" and suffix in str(args.get("code", "")):
                return True
            target = args.get("name") or args.get("tool_name") if name in ("tool_call", "use_mcp_tool") else name
            if str(target or "").lower().endswith(suffix.lower()):
                return True
        return False

    @staticmethod
    def _shell_arguments(schema: dict, command: str) -> dict:
        props = schema.get("properties") or {}
        if "commands" in props and "command" not in props and "cmd" not in props:
            return {"commands": [command]}         # cline's run_commands
        spec = props.get("command") or props.get("cmd") or props.get("CommandLine") or {}
        key = ("command" if "command" in props or not props else "cmd" if "cmd" in props
               else "CommandLine" if "CommandLine" in props else "command")
        value = ["bash", "-lc", command] if spec.get("type") == "array" else command
        args: dict = {key: value}
        for name in schema.get("required") or []:
            if name in args:
                continue
            kind = (props.get(name) or {}).get("type")
            args[name] = False if kind == "boolean" else 0 if kind in ("integer", "number") else (
                ("/workspace" if name.lower() in ("cwd", "workdir") else "Run the requested command")
                if kind == "string" else None)
        return {k: v for k, v in args.items() if v is not None}

    def _turn(self, tools: list, results: list) -> tuple:
        """``("text", text)`` or ``("tool", name, arguments)`` for one request.
        A ``("seq", [step, …])`` action plays its steps in turn, each step
        an action of its own: a step ends when its tool's result arrives."""
        if self.action[0] == "seq":
            return self._seq_turn(tools, results)
        if self.action[0] == "shell_all":
            return self._shell_all_turn(tools, results)
        return self._step_turn(tools, results)

    def _seq_turn(self, tools: list, results: list) -> tuple:
        if not tools:
            # A title or summary request neither advances nor ends the sequence.
            return ("text", "ok")
        steps = self.action[1]
        searched = bool(self.calls) and self.calls[-1][0] in ("tool_search", "search_tool_functions")
        if results and not searched and self._seq_started:
            self._seq_index += 1
            self._seq_results.extend(results)
        if self._seq_index >= len(steps):
            return ("text", "TOOL RESULT: " + " | ".join(self._seq_results))
        outer = self.action
        self.action = steps[self._seq_index]
        try:
            turn = self._step_turn(tools, results if searched else [])
        finally:
            self.action = outer
        if turn[0] == "tool":
            self._seq_started = True
        else:
            # A step whose tool is not offered ends the sequence here.
            self._seq_index = len(steps)
            turn = ("text", turn[1] + (" | " + " | ".join(self._seq_results) if self._seq_results else ""))
        return turn

    def _shell_all_turn(self, tools: list, results: list) -> tuple:
        """Call EVERY offered shell-like tool with ``command``, one per
        request — a deny row must not pass because only the first was tried."""
        if results:
            self._seq_results.extend(results)
        offered = [t for t in tools if _bare(_tool_name(t)) in SHELL_TOOLS]
        tried = {name for name, _args in self.calls}
        remaining = [t for t in offered if _tool_name(t) not in tried]
        if not remaining:
            names = sorted(_tool_name(t) for t in offered)
            return ("text", ("NO-SHELL" if not offered else f"TRIED {names}") +
                    (" | " + " | ".join(self._seq_results) if self._seq_results else ""))
        tool = remaining[0]
        picked = (_tool_name(tool), self._shell_arguments(_tool_schema(tool), self.action[1]))
        with self._lock:
            self.calls.append(picked)
        return ("tool", *picked)

    def _step_turn(self, tools: list, results: list) -> tuple:
        searched = bool(self.calls) and self.calls[-1][0] in ("tool_search", "search_tool_functions")
        self._last_results = " ".join(results)
        if results and not (searched and self.action[0] == "tool"):
            return ("text", "TOOL RESULT: " + " | ".join(results))
        if self.action[0] == "text":
            return ("text", self.action[1])
        picked = self._pick(tools) if tools else None
        if picked is None:
            names = sorted(_tool_name(t) for t in tools)
            if not tools:
                return ("text", "ok")
            if self.action[0] == "shell":
                return ("text", f"NO-SHELL: no shell tool among {names}")
            return ("text", f"NO MATCHING TOOL among {names}")
        with self._lock:
            self.calls.append(picked)
        return ("tool", *picked)

    # ── the wires ─────────────────────────────────────────────────────
    def _messages_turn(self, body: dict) -> tuple:
        # The results answering the model's last message: claude appends
        # system-reminder messages after them.
        messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        results = [_text(b.get("content")) for m in messages[last + 1:]
                   if isinstance(m.get("content"), list) for b in m["content"]
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        return self._turn(body.get("tools") or [], results)

    def _chat_turn(self, body: dict) -> tuple:
        # The results answering the model's last message: a harness may
        # append a reminder after them (qwen-code does).
        messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        results = [_text(m.get("content")) for m in messages[last + 1:] if m.get("role") == "tool"]
        return self._turn(body.get("tools") or [], results)

    def _responses_turn(self, body: dict) -> tuple:
        items = [i for i in (body.get("input") if isinstance(body.get("input"), list) else [])
                 if isinstance(i, dict)]
        made = ("function_call", "custom_tool_call", "local_shell_call", "message")
        last = max((n for n, i in enumerate(items) if i.get("type") in made
                    and (i.get("type") != "message" or i.get("role") == "assistant")), default=-1)
        results = [_text(i.get("output")) for i in items[last + 1:] if i.get("type") in (
            "function_call_output", "custom_tool_call_output", "local_shell_call_output")]
        tools = []
        for tool in body.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "namespace":
                # codex groups an MCP server's tools under one namespace.
                tools += [{**inner, "name": f"{tool.get('name')}__{inner.get('name')}"}
                          for inner in tool.get("tools") or [] if isinstance(inner, dict)]
            else:
                tools.append(tool)
        return self._turn(tools, results)

    def _gemini_turn(self, body: dict) -> tuple:
        # The results answering the model's last turn: the functionResponse
        # parts after the last ``model`` content.
        # (Antigravity's server sends a result back under role ``model`` too.)
        contents = [c for c in body.get("contents") or [] if isinstance(c, dict)]
        last = max((i for i, c in enumerate(contents)
                    if any(isinstance(p, dict) and "functionCall" in p for p in c.get("parts") or [])), default=-1)
        results = []
        for content in contents[last + 1:]:
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                response = part.get("functionResponse", {}).get("response")
                if response is not None:
                    results.append(_text(response.get("output", response.get("result", response)))
                                   if isinstance(response, dict) else _text(response))
                elif content.get("role") == "user" and part.get("text"):
                    results.clear()         # a later user prompt: the results were answered
        tools = [{"name": decl.get("name", ""),
                  "parameters": decl.get("parameters") or decl.get("parametersJsonSchema") or {}}
                 for tool in body.get("tools") or [] if isinstance(tool, dict)
                 for decl in tool.get("functionDeclarations") or [] if isinstance(decl, dict)]
        return self._turn(tools, results)

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _json(self, status: int, payload) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse_start(self) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "close")
                self.end_headers()
                self.close_connection = True

            def _event(self, data, name: str = "") -> None:
                head = f"event: {name}\n" if name else ""
                body = data if isinstance(data, str) else json.dumps(data)
                self.wfile.write(f"{head}data: {body}\n\n".encode())
                self.wfile.flush()

            def _served(self) -> "str | None":
                """The request path when it lies under the mock's prefix;
                otherwise a 404, kept as a miss."""
                path = self.path.split("?", 1)[0]
                rest = path[len(mock.prefix):] if path.startswith(mock.prefix + "/") else ""
                # Only the wires' own paths directly under the prefix: a
                # doubled segment (``/v1/v1/messages``) is a wrong URL shape.
                if rest in _API_PATHS or rest.startswith("/models"):
                    return path
                if not path.endswith(_PROBES):
                    with mock._lock:
                        mock.misses.append((self.command, self.path))
                self._json(404, {"error": {"type": "not_found_error", "message": self.path}})
                return None

            def do_GET(self) -> None:
                path = self._served()
                if path is None:
                    return
                if path.endswith("/models"):
                    model = {"id": "mock-model", "object": "model", "created": 0, "owned_by": "mock",
                             "type": "model", "display_name": "Mock", "created_at": "2026-01-01T00:00:00Z"}
                    self._json(200, {"object": "list", "data": [model], "has_more": False,
                                     "first_id": "mock-model", "last_id": "mock-model"})
                    return
                self._json(404, {"error": {"type": "not_found_error", "message": self.path}})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                path = self._served()
                if path is None:
                    return
                if path.endswith("/count_tokens"):
                    self._json(200, {"input_tokens": 10})
                elif path.endswith("/messages"):
                    self._record("anthropic", body)
                    self._anthropic(body, mock._messages_turn(body))
                elif path.endswith("/chat/completions"):
                    self._record("chat", body)
                    self._chat(body, mock._chat_turn(body))
                elif path.endswith("/responses"):
                    self._record("responses", body)
                    self._responses(body, mock._responses_turn(body))
                elif path.endswith((":generateContent", ":streamGenerateContent")):
                    self._record("gemini", body)
                    self._gemini(path.endswith(":streamGenerateContent"), mock._gemini_turn(body))
                else:
                    self._json(404, {"error": {"type": "not_found_error", "message": self.path}})

            def _record(self, wire: str, body: dict) -> None:
                with mock._lock:
                    mock.requests.append((wire, body))

            # Anthropic Messages
            def _anthropic(self, body: dict, turn: tuple) -> None:
                n = uuid.uuid4().hex[:12]
                if turn[0] == "tool":
                    blocks = [{"type": "tool_use", "id": f"toolu_{n}", "name": turn[1], "input": turn[2]}]
                    stop = "tool_use"
                else:
                    blocks, stop = [{"type": "text", "text": turn[1]}], "end_turn"
                message = {"id": f"msg_{n}", "type": "message", "role": "assistant",
                           "model": body.get("model") or "mock-model", "content": blocks,
                           "stop_reason": stop, "stop_sequence": None,
                           "usage": {"input_tokens": 10, "output_tokens": 5}}
                if not body.get("stream"):
                    self._json(200, message)
                    return
                self._sse_start()
                self._event({"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
                            "message_start")
                for i, block in enumerate(blocks):
                    if block["type"] == "tool_use":
                        start = {**block, "input": {}}
                        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                    else:
                        start = {"type": "text", "text": ""}
                        delta = {"type": "text_delta", "text": block["text"]}
                    self._event({"type": "content_block_start", "index": i, "content_block": start},
                                "content_block_start")
                    self._event({"type": "content_block_delta", "index": i, "delta": delta}, "content_block_delta")
                    self._event({"type": "content_block_stop", "index": i}, "content_block_stop")
                self._event({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 5}}, "message_delta")
                self._event({"type": "message_stop"}, "message_stop")

            # OpenAI Chat Completions
            def _chat(self, body: dict, turn: tuple) -> None:
                n = uuid.uuid4().hex[:12]
                model = body.get("model") or "mock-model"
                usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                if turn[0] == "tool":
                    call = {"id": f"call_{n}", "type": "function",
                            "function": {"name": turn[1], "arguments": json.dumps(turn[2])}}
                    message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
                else:
                    message, finish = {"role": "assistant", "content": turn[1]}, "stop"
                base = {"id": f"chatcmpl-{n}", "created": 0, "model": model}
                if not body.get("stream"):
                    self._json(200, {**base, "object": "chat.completion", "usage": usage, "choices": [
                        {"index": 0, "message": message, "finish_reason": finish, "logprobs": None}]})
                    return
                self._sse_start()

                def chunk(delta: dict, finish_reason=None, **extra) -> None:
                    self._event({**base, "object": "chat.completion.chunk", **extra, "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}]})

                if turn[0] == "tool":
                    call = message["tool_calls"][0]
                    chunk({"role": "assistant", "content": None, "tool_calls": [
                        {"index": 0, "id": call["id"], "type": "function",
                         "function": {"name": call["function"]["name"], "arguments": ""}}]})
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": call["function"]["arguments"]}}]})
                else:
                    chunk({"role": "assistant", "content": ""})
                    chunk({"content": turn[1]})
                chunk({}, finish)
                if (body.get("stream_options") or {}).get("include_usage"):
                    self._event({**base, "object": "chat.completion.chunk", "choices": [], "usage": usage})
                self._event("[DONE]")

            # Gemini generateContent
            def _gemini(self, streamed: bool, turn: tuple) -> None:
                if turn[0] == "tool":
                    part = {"functionCall": {"name": turn[1], "args": turn[2]}}
                else:
                    part = {"text": turn[1]}
                response = {"candidates": [{"content": {"role": "model", "parts": [part]},
                                            "finishReason": "STOP", "index": 0}],
                            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                                              "totalTokenCount": 15},
                            "modelVersion": "mock-model"}
                if not streamed:
                    self._json(200, response)
                    return
                self._sse_start()
                self._event(response)

            # OpenAI Responses
            def _responses(self, body: dict, turn: tuple) -> None:
                n = uuid.uuid4().hex[:12]
                if turn[0] == "tool":
                    item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
                            "name": turn[1], "arguments": json.dumps(turn[2]), "status": "completed"}
                    namespace, _, name = turn[1].rpartition("__")
                    if turn[1].startswith("mcp__") and namespace:
                        # A namespaced tool is called by name within its namespace.
                        item = {**item, "name": name, "namespace": namespace}
                else:
                    item = {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": turn[1], "annotations": []}]}
                usage = {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5,
                         "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}
                response = {"id": f"resp_{n}", "object": "response", "created_at": 0, "status": "completed",
                            "model": body.get("model") or "mock-model", "output": [item], "usage": usage,
                            "error": None, "incomplete_details": None}
                if not body.get("stream"):
                    self._json(200, response)
                    return
                self._sse_start()
                seq = iter(range(1_000_000))

                def event(kind: str, **data) -> None:
                    self._event({"type": kind, "sequence_number": next(seq), **data}, kind)

                event("response.created", response={**response, "status": "in_progress", "output": []})
                event("response.in_progress", response={**response, "status": "in_progress", "output": []})
                if item["type"] == "function_call":
                    event("response.output_item.added", output_index=0,
                          item={**item, "arguments": "", "status": "in_progress"})
                    event("response.function_call_arguments.delta", item_id=item["id"], output_index=0,
                          delta=item["arguments"])
                    event("response.function_call_arguments.done", item_id=item["id"], output_index=0,
                          arguments=item["arguments"])
                else:
                    text = item["content"][0]["text"]
                    event("response.output_item.added", output_index=0,
                          item={**item, "status": "in_progress", "content": []})
                    part = {"type": "output_text", "text": "", "annotations": []}
                    event("response.content_part.added", item_id=item["id"], output_index=0, content_index=0,
                          part=part)
                    event("response.output_text.delta", item_id=item["id"], output_index=0, content_index=0,
                          delta=text)
                    event("response.output_text.done", item_id=item["id"], output_index=0, content_index=0,
                          text=text)
                    event("response.content_part.done", item_id=item["id"], output_index=0, content_index=0,
                          part={**part, "text": text})
                event("response.output_item.done", output_index=0, item=item)
                event("response.completed", response=response)

        return Handler
