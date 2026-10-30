# SPDX-License-Identifier: MIT
"""A scripted Anthropic Messages server for driving the REAL claude CLI
through its permission flow without billing an account.

Every request that offers tools (the agent's main loop) is answered with a
``Bash`` tool_use running :attr:`AnthropicMock.command` — whether or not the
request offered Bash, so a withheld tool is exercised too; once the request
carries that tool's result, the reply echoes it back as text. Requests without
tools (the CLI's side queries) get a short text reply. Streaming (SSE) and
plain JSON responses are both served; every request body is kept in
:attr:`AnthropicMock.requests`."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _text_of(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


class AnthropicMock:
    def __init__(self, command: str) -> None:
        self.command = command
        self.requests: list = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "AnthropicMock":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── the scripted turn ──────────────────────────────────────────────
    def _reply(self, body: dict) -> tuple:
        """``(content blocks, stop_reason)`` for one request."""
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        content = last.get("content") if isinstance(last, dict) else None
        results = [b for b in content or [] if isinstance(b, dict)
                   and b.get("type") == "tool_result"] if isinstance(content, list) else []
        if results:
            text = "TOOL RESULT: " + " | ".join(_text_of(r) for r in results)
            return [{"type": "text", "text": text}], "end_turn"
        if body.get("tools"):
            n = len(self.requests)
            return [{"type": "tool_use", "id": f"toolu_mock{n:04d}", "name": "Bash",
                     "input": {"command": self.command,
                               "description": "Run the requested command"}}], "tool_use"
        return [{"type": "text", "text": "ok"}], "end_turn"

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._json(404, {"type": "error", "error": {
                    "type": "not_found_error", "message": self.path}})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                path = self.path.split("?", 1)[0]
                if path.endswith("/count_tokens"):
                    self._json(200, {"input_tokens": 10})
                    return
                if not path.endswith("/v1/messages"):
                    self._json(404, {"type": "error", "error": {
                        "type": "not_found_error", "message": self.path}})
                    return
                mock.requests.append(body)
                blocks, stop = mock._reply(body)
                model = body.get("model") or "claude-mock"
                usage = {"input_tokens": 10, "output_tokens": 5}
                message = {"id": f"msg_mock{len(mock.requests):04d}", "type": "message",
                           "role": "assistant", "model": model, "content": blocks,
                           "stop_reason": stop, "stop_sequence": None, "usage": usage}
                if not body.get("stream"):
                    self._json(200, message)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()

                def event(name: str, data: dict) -> None:
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

                event("message_start", {"type": "message_start", "message": {
                    **message, "content": [], "stop_reason": None}})
                for i, block in enumerate(blocks):
                    if block["type"] == "tool_use":
                        event("content_block_start", {"type": "content_block_start", "index": i,
                              "content_block": {**block, "input": {}}})
                        delta = {"type": "input_json_delta",
                                 "partial_json": json.dumps(block["input"])}
                    else:
                        event("content_block_start", {"type": "content_block_start", "index": i,
                              "content_block": {"type": "text", "text": ""}})
                        delta = {"type": "text_delta", "text": block["text"]}
                    event("content_block_delta", {"type": "content_block_delta",
                                                  "index": i, "delta": delta})
                    event("content_block_stop", {"type": "content_block_stop", "index": i})
                event("message_delta", {"type": "message_delta",
                                        "delta": {"stop_reason": stop, "stop_sequence": None},
                                        "usage": {"output_tokens": 5}})
                event("message_stop", {"type": "message_stop"})
                self.wfile.flush()

        return Handler
