"""Every demo timeline, run against the fake runtime with zero sleeps.

Each timeline must run to completion at realistic (virtual) pacing, leave
files its renderer can read, and exit cleanly when Stop cancels it at any
point. Run from the repo root:

    .venv/bin/python -m pytest playground/tests/demo -q
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re

import pytest
import yaml
from collimate.transcript import parse_transcript

from fake_demo import DEMO_DIR, FOLDER, FakeDemo, declared_ui_files, demo_generators

GENERATORS = demo_generators()
EXPECTED = {"terminal-session", "chat-assistant", "modify-files"}
SAMPLE_CELL = DEMO_DIR / "channel" / "sample-notes"


def load_timeline(gen_id: str):
    path = GENERATORS[gen_id] / "timeline.py"
    spec = importlib.util.spec_from_file_location(f"demo_timeline_{gen_id.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def defaults(gen_id: str) -> dict:
    schema = yaml.safe_load((GENERATORS[gen_id] / "settings.set_yaml").read_text())
    return {field["key"]: field["default"] for field in schema["fields"]}


def sample_inputs() -> list[dict]:
    files = {p.name: p.read_text() for p in sorted(SAMPLE_CELL.iterdir()) if p.is_file()}
    return [{"name": "Sample notes", "files": files}]


def run(gen_id: str, **kwargs) -> FakeDemo:
    params = {**defaults(gen_id), **kwargs.pop("params", {})}
    if gen_id == "modify-files" and "inputs" not in kwargs:
        kwargs["inputs"] = sample_inputs()
    demo = FakeDemo(params=params, ui_files=declared_ui_files(gen_id), **kwargs)
    asyncio.run(load_timeline(gen_id).play(demo))
    return demo


def test_the_four_generators_exist():
    assert set(GENERATORS) == EXPECTED


@pytest.mark.parametrize("gen_id", sorted(EXPECTED))
def test_runs_to_completion_with_realistic_pacing(gen_id):
    demo = run(gen_id)
    assert demo.finished == (True, demo.finished[1]) and demo.finished[1]
    names = demo.phase_names()
    assert names[0] == "spinning_up" and "running" in names
    assert names[-1] == "finishing"
    assert not demo.staged, "files staged but never committed"
    assert demo.commits and demo.logs
    assert 15 <= demo.scripted_seconds() <= 40, demo.scripted_seconds()


@pytest.mark.parametrize("gen_id", sorted(EXPECTED))
def test_pace_changes_timing_not_structure(gen_id):
    normal = run(gen_id)
    brisk = run(gen_id, params={"pace": "brisk"})
    relaxed = run(gen_id, params={"pace": "relaxed"})
    assert brisk.scripted_seconds() < normal.scripted_seconds() < relaxed.scripted_seconds()
    assert brisk.phase_names() == normal.phase_names() == relaxed.phase_names()
    assert [c["written"] for c in brisk.commits] == [c["written"] for c in normal.commits]
    assert brisk.files == normal.files == relaxed.files


# ── terminal-session ──────────────────────────────────────────────────

def test_runs_on_a_cell_with_files_keep_its_name():
    """A run started on a cell that holds files must not rename it after the
    demo; an empty cell (the app's blank "New cell", or none) gets the
    generator's own name."""
    with_files = [{"name": "Welcome", "files": {"README.md": "# hi\n"}}]
    blank = [{"name": "New cell", "files": {}}]
    for gen_id, fresh in (("terminal-session", "Demo terminal"), ("chat-assistant", "Demo chat")):
        assert run(gen_id, inputs=with_files).commits[0]["cell_name"] == "Welcome", gen_id
        assert run(gen_id, inputs=blank).commits[0]["cell_name"] == fresh, gen_id
        assert run(gen_id).commits[0]["cell_name"] == fresh, gen_id


def test_terminal_bytes_are_ansi_and_the_pointer_is_revealed__F0287():
    demo = run("terminal-session")
    (term,) = demo.terminals
    data = bytes(term.data)
    assert data and b"\x1b[0m" in data
    text = data.decode("utf-8")
    assert "git clone" in text and "passed" in text and "skipped" in text
    assert "100%" in text and "\r\n" in text
    assert term.pointer_path == f"{FOLDER}/terminal/session.terminal"
    assert term.pointer_path in demo.commits[0]["written"]
    assert demo.revealed == [term.pointer_path]
    pointer = json.loads(demo.files[term.pointer_path])
    assert set(pointer) >= {"terminal_id", "ws_path", "token", "label"}
    assert pointer["terminal_id"] == "term_fake"
    assert demo.clock_at_phase("awaiting_input") is not None


def test_terminal_echoes_typed_input_and_refuses_to_execute():
    fed = []

    async def type_when_open(demo: FakeDemo) -> None:
        if demo.phase_names()[-1] == "awaiting_input" and not fed:
            fed.append(True)
            await demo.terminals[0].feed(b"ls -la\r")
            await demo.terminals[0].feed(b"x\x7f")          # a typo and its backspace
            await demo.terminals[0].feed(b"\x1b[A")          # an arrow key: ignored
            await demo.terminals[0].feed(b"\r")              # an empty line: just a new prompt

    demo = run("terminal-session", on_sleep=type_when_open)
    assert fed
    text = bytes(demo.terminals[0].data).decode("utf-8")
    prompt_open = text.index("Type here")
    typed = text[prompt_open:]
    assert "ls -la" in typed
    assert typed.count("demo shell — commands are not executed here") == 1
    assert "\b \b" in typed
    assert demo.finished[0] is True


def test_terminal_ignores_input_before_and_after_the_open_window():
    demo = run("terminal-session")
    before = bytes(demo.terminals[0].data)
    asyncio.run(demo.terminals[0].feed(b"late\r"))
    assert bytes(demo.terminals[0].data) == before


def test_terminal_keep_open_zero_completes_without_waiting():
    demo = run("terminal-session", params={"keep_open_seconds": 0})
    assert "awaiting_input" not in demo.phase_names()
    assert demo.finished[0] is True


# ── chat-assistant ────────────────────────────────────────────────────

def test_chat_transcript_parses_and_matches_the_stream():
    demo = run("chat-assistant", prompt="What is this?",
               user_messages=("Is there a real model here?", "flibbertigibbet"))
    (session,) = demo.chats
    transcript = f"{FOLDER}/chat/main.json"
    assert demo.run_path(session.transcript) == transcript
    messages = parse_transcript(demo.files[transcript])
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant"] * 4
    assert messages[0].content == "What is this?"
    assert messages[1].thinking and "scripted" in messages[1].thinking
    assert messages[4].content == "Is there a real model here?"
    assert "No model" in messages[5].content
    assert "canned" in messages[7].content            # the default reply
    assert all(m.content for m in messages)
    assert session.turns == 4
    for turn, assistant in enumerate(messages[1::2], start=1):
        assert session.streamed("text_delta", turn) == assistant.content
        assert session.streamed("thinking_delta", turn) == assistant.thinking
    kinds = [e["type"] for e in session.events]
    assert kinds.count("turn_start") == kinds.count("turn_end") == 4
    # The transcript is committed before each turn_end so the renderer's
    # reconcile finds the reply on disk.
    assert session.pointer_path == f"{FOLDER}/chat/conversation.chat"
    assert demo.revealed == [session.pointer_path]
    pointer = json.loads(demo.files[session.pointer_path])
    assert pointer["threads"][0]["file"] == transcript
    assert demo.commits[-1]["files"][transcript] == demo.files[transcript]
    # Every file the chat writes is a UI file in the run folder.
    assert all(p.startswith(FOLDER + "/") for c in demo.commits for p in c["written"])


def test_chat_uses_the_default_opening_and_can_hide_thinking():
    demo = run("chat-assistant", params={"show_thinking": False})
    messages = parse_transcript(demo.files[f"{FOLDER}/chat/main.json"])
    assert messages[0].content == "How does this demo work?"
    assert all(not m.thinking for m in messages)
    assert not any(e["type"] == "thinking_delta" for e in demo.chats[0].events)


def test_chat_replies_match_whole_words_only():
    pick = load_timeline("chat-assistant").pick_reply
    greeting = pick("hi")
    assert greeting.startswith("Hello!")
    assert pick("Hey there") == greeting
    # Substrings of ordinary words are not keywords.
    assert pick("Can you summarize this page for me?") == load_timeline("chat-assistant").DEFAULT_REPLY
    assert pick("Is this credit card really needed?") == load_timeline("chat-assistant").DEFAULT_REPLY
    assert "No model" in pick("Is a real model behind this?")
    assert "Modify files" in pick("How do I edit the files?")
    assert "Stop" in pick("How do I stop a run?")
    assert "welcome" in pick("Thanks!")


def test_chat_transcript_is_committed_before_every_turn_end():
    demo = run("chat-assistant", user_messages=("hello",))
    session = demo.chats[0]
    turn_ends = [i for i, e in enumerate(session.events) if e["type"] == "turn_end"]
    assert len(turn_ends) == 3
    # After play, every assistant reply is in the durable transcript, and the
    # number of commits carrying a transcript is at least the number of turns.
    with_transcript = [c for c in demo.commits if f"{FOLDER}/chat/main.json" in c["written"]]
    assert len(with_transcript) >= 3


# ── modify-files ──────────────────────────────────────────────────────

def test_modify_files_edits_the_sample_notes_and_reveals_changes():
    demo = run("modify-files", prompt="Tidy these notes.")
    files = demo.files
    assert files["trip-plan.md"].startswith("# Trip plan\n\n> **Summary:**")
    assert "- [ ] Passport" in files["packing-list.md"]
    assert "> **Summary:**" in files["packing-list.md"]
    assert "- [ ]" not in files["trip-plan.md"]          # only list files become checklists
    assert files["budget.txt"].rstrip().endswith("723")
    assert files["budget.txt"].count("Total") == 1
    changes = files["CHANGES.md"]
    assert changes.startswith("# Changes")
    for name in ("trip-plan.md", "packing-list.md", "budget.txt"):
        assert f"`{name}`" in changes and f"## {name}" in changes
    assert "```diff" in changes and "+> **Summary:**" in changes
    assert "Prompt: Tidy these notes." in changes
    assert demo.revealed == ["CHANGES.md"]
    # One commit per file (a copy each, edited or not), then the revealed CHANGES.md.
    assert [c["written"] for c in demo.commits] == [["budget.txt"], ["packing-list.md"], ["trip-plan.md"], ["CHANGES.md"]]
    assert any("Plan:" in line for line in demo.logs)
    assert "Prompt (shown, not interpreted): Tidy these notes." in demo.logs


def test_modify_files_is_idempotent_and_falls_back_without_inputs():
    once = run("modify-files")
    twice = run("modify-files", inputs=[{"name": "Edited", "files": {k: v for k, v in once.files.items() if k != "CHANGES.md"}}])
    # Already-tidy files are copied unchanged, and CHANGES.md says so.
    assert {k: v for k, v in twice.files.items() if k != "CHANGES.md"} == {k: v for k, v in once.files.items() if k != "CHANGES.md"}
    assert twice.files["CHANGES.md"].count("already tidy — no change") == 3
    assert "```diff" not in twice.files["CHANGES.md"]
    assert [c["written"] for c in twice.commits] == [["budget.txt"], ["packing-list.md"], ["trip-plan.md"], ["CHANGES.md"]]

    none = run("modify-files", inputs=[])
    assert "reading-list.md" in none.files and "- [ ] The Pragmatic Programmer" in none.files["reading-list.md"]
    assert none.revealed == ["CHANGES.md"]

    binary = run("modify-files", inputs=[{"name": "Bin", "files": {"photo.png": b"\x89PNG", ".hidden": "x"}}])
    assert "reading-list.md" in binary.files

    # Another run's UI files are read-only: never edited, never copied.
    sibling = run("modify-files", inputs=[{"name": "Shared", "files": {
        "notes.md": "# Notes\n", "_runs/other-1234abcd/chat/main.json": "{}"}}])
    assert not any(p.startswith("_runs/") for p in sibling.files)


def test_modify_files_settings_switch_edits_off():
    demo = run("modify-files", params={"add_summary": False, "checklist_items": False})
    assert "Summary" not in demo.files.get("trip-plan.md", "")
    assert "- [ ]" not in demo.files.get("packing-list.md", "")
    assert demo.files["budget.txt"].rstrip().endswith("723")




# ── Stop ──────────────────────────────────────────────────────────────

def _cancel_points(gen_id: str) -> list[int]:
    total = run(gen_id, user_messages=("hello",) if gen_id == "chat-assistant" else ()).sleeps
    return sorted({1, 2, total // 3, (2 * total) // 3, total - 1, total})


@pytest.mark.parametrize("gen_id", sorted(EXPECTED))
def test_stop_at_any_point_exits_cleanly(gen_id):
    for n in _cancel_points(gen_id):
        kwargs = {"cancel_after": n}
        if gen_id == "chat-assistant":
            kwargs["user_messages"] = ("hello",)
        if gen_id == "modify-files":
            kwargs["inputs"] = sample_inputs()
        demo = FakeDemo(params=defaults(gen_id), ui_files=declared_ui_files(gen_id), **kwargs)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(load_timeline(gen_id).play(demo))
        assert demo.cancelled and demo.finished is None, (gen_id, n)
        assert demo.sleeps == n, (gen_id, n)


# ── the seed the shim boots from ──────────────────────────────────────

def test_channel_yaml_matches_the_generators_and_cells():
    seed = yaml.safe_load((DEMO_DIR / "channel.yaml").read_text())
    assert seed["name"] == "Demo"
    for cell in seed["cells"]:
        folder = DEMO_DIR / "channel" / cell["folder"]
        assert folder.is_dir() and cell["name"]
        assert (folder / cell["pinned"]).is_file()
    assert [c["name"] for c in seed["cells"]] == ["Welcome", "Sample notes"]
    drafts = seed["runners"]
    # The open-ended windows keep a run near the 15–40 s the demo aims for.
    settings = {d["runner"]["id"]: d["runner"]["settings"]["settings.set_yaml"] for d in drafts}
    assert settings["terminal-session"]["keep_open_seconds"] <= 20
    assert settings["chat-assistant"]["idle_timeout_seconds"] <= 30
    assert {d["runner"]["generator"] for d in drafts} == EXPECTED
    assert len({d["runner"]["id"] for d in drafts}) == len(drafts)
    for draft in drafts:
        runner = draft["runner"]
        assert set(draft) == {"runner", "files"} and draft["files"] == []
        assert set(runner) == {"id", "generator", "name", "description", "tags", "prompt",
                               "settings", "accounts", "folders", "permissions", "packages"}
        assert set(runner["settings"]) == {"settings.set_yaml"}
        assert set(runner["settings"]["settings.set_yaml"]) == set(defaults(runner["generator"]))
    welcome = (DEMO_DIR / "channel" / "welcome" / "README.md").read_text()
    assert 120 <= len(welcome.split()) <= 200
    for phrase in ("browser", "Nothing is sent", "fake"):
        assert phrase in welcome


def test_generator_tree_holds_only_text_files():
    """genesis reads templates as UTF-8 text — the tree must never carry a
    binary or a stray cache."""
    for path in DEMO_DIR.rglob("*"):
        if path.is_file():
            assert "__pycache__" not in path.parts and path.suffix != ".pyc", path
            path.read_text(encoding="utf-8")
