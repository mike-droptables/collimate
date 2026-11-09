"""A fake of the playground's timeline runtime contract, with zero sleeps.

Timelines under ``playground/demo/generators/<cell>/<gen>/timeline.py`` code
against the ``demo`` object the shim hands ``play(demo)``. This fake
implements that contract as a recorder: virtual time advances by the seconds
each ``sleep`` asks for, commits snapshot the staged files, and the live
surfaces (chat, terminal) capture what a timeline sends them. It also
enforces the contract's rules — a delta outside a turn, a work file under
``_runs/``, a UI file of an undeclared type, a reveal of a file the cell
does not hold, a finish after finish — so a timeline that would misbehave
against the real shim fails here first.

The run folder, ``_runs/<label>/``, is the shim's; the pointer files use the
SDK's canonical builders (``collimate.pointers``), as the shim does.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import yaml
from collimate.pointers import chat_session_pointer, terminal_pointer

ACTIVE_PHASES = ("queued", "spinning_up", "running", "awaiting_input", "finishing")

REPO = Path(__file__).resolve().parents[3]
DEMO_DIR = REPO / "playground" / "demo"
GENERATORS_DIR = DEMO_DIR / "generators"

_SAFE_PATH = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[^\0]+$")

LABEL = "demo-runner-0a1b2c3d"
FOLDER = f"_runs/{LABEL}"


def demo_generators() -> dict[str, Path]:
    """``{generator id: folder}`` for every generator in the demo tree."""
    out = {}
    for manifest in sorted(GENERATORS_DIR.glob("*/*/definition.generator")):
        out[manifest.parent.name] = manifest.parent
    return out


def declared_ui_files(gen_id: str) -> tuple[str, ...]:
    """The generator manifest's ``ui_files`` suffixes."""
    manifest = yaml.safe_load((demo_generators()[gen_id] / "definition.generator").read_text())
    return tuple(manifest.get("ui_files") or ())


class FakeChatSession:
    def __init__(self, demo: "FakeDemo", label: str):
        self.demo = demo
        self.label = label
        self.transcript = "chat/main.json"
        self.events: list[dict] = []
        self.inbox: list[str] = list(demo.user_messages)
        self.in_turn = False
        self.turns = 0
        self.pointer_path = demo.update_ui_file("chat/conversation.chat", json.dumps(chat_session_pointer(
            {"chat_id": "chat_fake", "ws_path": "/api/chats/chat_fake", "token": "t"},
            threads_dir=demo.run_path("chat"), label=label)))

    async def start_turn(self) -> None:
        assert not self.in_turn, "start_turn while a turn is open"
        self.in_turn = True
        self.turns += 1
        self.events.append({"type": "turn_start", "turn": self.turns})

    async def delta(self, text: str) -> None:
        assert self.in_turn, "delta outside a turn"
        assert isinstance(text, str) and text
        self.events.append({"type": "text_delta", "turn": self.turns, "text": text})

    async def thinking(self, text: str) -> None:
        assert self.in_turn, "thinking outside a turn"
        assert isinstance(text, str) and text
        self.events.append({"type": "thinking_delta", "turn": self.turns, "text": text})

    async def end_turn(self) -> None:
        assert self.in_turn, "end_turn without a turn"
        self.in_turn = False
        self.events.append({"type": "turn_end", "turn": self.turns})

    async def wait_user_message(self) -> str | None:
        assert not self.in_turn, "waiting for a message while a turn is open"
        if self.inbox:
            return self.inbox.pop(0)
        return None

    def streamed(self, kind: str, turn: int) -> str:
        return "".join(e["text"] for e in self.events if e["type"] == kind and e["turn"] == turn)


class FakeTerminal:
    def __init__(self, demo: "FakeDemo", label: str, name: str):
        self.label = label
        self.data = bytearray()
        self.callback = None
        self.pointer_path = demo.update_ui_file(name, json.dumps(terminal_pointer(
            {"terminal_id": "term_fake", "ws_path": "/api/demo/terminals/term_fake", "token": "t"}, label=label)))

    async def write(self, data) -> None:
        assert isinstance(data, (bytes, bytearray, str))
        self.data += data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def on_input(self, callback) -> None:
        assert callable(callback)
        self.callback = callback

    async def feed(self, data: bytes) -> None:
        """What the shim does with bytes the renderer sends: call the
        callback; await its result when it hands one back."""
        assert self.callback is not None, "no input handler registered"
        result = self.callback(data)
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result


class FakeDemo:
    def __init__(self, *, prompt: str = "", params: dict | None = None, inputs: list | None = None,
                 cancel_after: int | None = None, user_messages: tuple[str, ...] = (), on_sleep=None,
                 ui_files: tuple[str, ...] = ()):
        self.prompt = prompt
        self.label = LABEL
        self.folder = FOLDER
        self.ui_files = tuple(ui_files)
        self.params = dict(params or {})
        self.inputs = list(inputs or [])
        self.workspace = "/fake/workspace"
        self.cancelled = False
        self.user_messages = user_messages
        self.cancel_after = cancel_after
        self.on_sleep = on_sleep
        self.clock = 0.0
        self.sleeps = 0
        self.logs: list[str] = []
        self.phases: list[tuple[str, str, float]] = []
        self.staged: dict[str, str | bytes] = {}
        self.files: dict[str, str | bytes] = {}
        self.commits: list[dict] = []
        self.revealed: list[str] = []
        self.finished: tuple[bool, str] | None = None
        self.chats: list[FakeChatSession] = []
        self.terminals: list[FakeTerminal] = []

    # ── the contract ────────────────────────────────────────────────────
    def log(self, line: str) -> None:
        assert isinstance(line, str)
        self.logs.append(line)

    def phase(self, name: str, message: str = "") -> None:
        assert name in ACTIVE_PHASES, f"unknown phase {name!r}"
        assert isinstance(message, str)
        self.phases.append((name, message, self.clock))

    def run_path(self, name: str) -> str:
        return f"{self.folder}/{name}"

    def write(self, path: str, content) -> None:
        assert isinstance(path, str) and _SAFE_PATH.match(path), f"unsafe path {path!r}"
        assert not path.startswith("_runs/"), f"{path} is under _runs/: use demo.update_ui_file"
        assert isinstance(content, (str, bytes)), f"content of {path} must be str or bytes"
        self.staged[path] = content

    def update_ui_file(self, name: str, content) -> str:
        assert isinstance(name, str) and _SAFE_PATH.match(name), f"unsafe UI file name {name!r}"
        assert any(name.endswith(s) for s in self.ui_files), \
            f"{name}: its type is not in the manifest's ui_files {list(self.ui_files)}"
        assert not name.endswith(".run"), "the run card is the host's"
        assert isinstance(content, (str, bytes)), f"content of {name} must be str or bytes"
        path = self.run_path(name)
        self.staged[path] = content
        return path

    async def commit(self, cell_name: str | None = None) -> None:
        assert self.finished is None, "commit after finish"
        self.files.update(self.staged)
        self.commits.append({"clock": self.clock, "files": dict(self.files), "written": sorted(self.staged),
                             "cell_name": cell_name})
        self.staged.clear()

    async def reveal_file(self, path: str) -> None:
        if path in self.staged:
            await self.commit()
        assert path in self.files, f"revealed {path!r}, which the cell does not hold"
        self.revealed.append(path)

    async def sleep(self, seconds: float) -> None:
        assert isinstance(seconds, (int, float)) and seconds >= 0, f"bad sleep {seconds!r}"
        self.sleeps += 1
        self.clock += float(seconds)
        if self.cancel_after is not None and self.sleeps >= self.cancel_after:
            self.cancelled = True
            raise asyncio.CancelledError()
        if self.on_sleep is not None:
            await self.on_sleep(self)

    def finish(self, success: bool = True, message: str = "") -> None:
        assert self.finished is None, "finish called twice"
        assert isinstance(success, bool) and isinstance(message, str)
        self.finished = (success, message)

    def chat_session(self, label: str) -> FakeChatSession:
        session = FakeChatSession(self, label)
        self.chats.append(session)
        return session

    def terminal(self, label: str, name: str = "terminal/session.terminal") -> FakeTerminal:
        term = FakeTerminal(self, label, name)
        self.terminals.append(term)
        return term

    # ── helpers for assertions ──────────────────────────────────────────
    def phase_names(self) -> list[str]:
        return [name for name, _, _ in self.phases]

    def clock_at_phase(self, name: str) -> float | None:
        for phase, _, clock in self.phases:
            if phase == name:
                return clock
        return None

    def scripted_seconds(self) -> float:
        """Virtual seconds until the timeline first waits for the visitor
        (``awaiting_input``) or, failing that, until it finished."""
        waited = self.clock_at_phase("awaiting_input")
        return self.clock if waited is None else waited
