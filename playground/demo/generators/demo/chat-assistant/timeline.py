"""Chat assistant — a scripted conversation in the assistant renderer.

Two canned turns stream word by word (the first with a short thinking
section), then the chat stays open: each message the visitor sends gets one
of a few replies picked by keyword, or a default that explains the demo.
Every turn is also appended to the transcript ``session.transcript`` — a
UI file in the run's folder, in the shape ``collimate.transcript`` reads —
which is what the renderer reconciles from at the end of a turn. The chat
lives as long as the run: when it ends the host sweeps the run folder and
the transcript stays in the run's Logs.

No model is called; nothing leaves the browser.
"""
from __future__ import annotations

import asyncio
import json
import re

SPINNING_UP = "spinning_up"
RUNNING = "running"
AWAITING_INPUT = "awaiting_input"
FINISHING = "finishing"

PACE = {"relaxed": 1.6, "normal": 1.0, "brisk": 0.55}

OPENING = "How does this demo work?"

THINKING = (
    "The visitor is asking about the demo itself. I should explain what is "
    "real here and what is scripted, keep it short, and point at the other "
    "demo generators so they know what to try next."
)

FIRST_REPLY = (
    "Everything you see is the real Collimate app, running entirely in your "
    "browser. What is *not* real is me: this reply is a script, streamed word "
    "by word the way a model's answer would be.\n\n"
    "Here is what the demo shows:\n\n"
    "- **Live surfaces** — this chat and the terminal are the same renderers "
    "a real run uses.\n"
    "- **Runs and versions** — each generator writes files into a cell, and "
    "every write is a version you can step back through.\n"
    "- **The runner picker** — the three demo runners are ordinary saved runners; "
    "open one to see its settings.\n\n"
    "Nothing leaves the browser and nothing is saved when you close the tab."
)

FOLLOW_UP = "What would a real run do differently?"

SECOND_REPLY = (
    "A real run starts a generator process on your machine or in a container. "
    "The **Agent** generator, for example, connects to the model account you "
    "chose in the Key manager, reads the input cell, and edits files or "
    "answers here as the model decides.\n\n"
    "The wiring is identical: the generator streams turns over the same "
    "channel this script uses, and commits files the same way. Try the "
    "**Modify files** runner on the *Sample notes* cell to see a scripted "
    "edit land as a new version, or ask me something — I have a few canned "
    "answers left."
)

# Keywords → reply. The first entry with a keyword that appears in the message
# as a whole word wins (so "hi" never matches "this", nor "edit" "credit");
# order is deliberate (specific before general).
SCRIPTED_REPLIES = (
    (("stop",), "The **Stop** button on the run bar ends any run at once — this chat included. "
                "The cell keeps everything written so far, marked as a stopped run."),
    (("real", "model"), "No model is behind this chat. The playground swaps every generator for a "
                        "script, so the words are canned — the plumbing they travel through is real."),
    (("file", "files", "edit", "edits", "editing"), "Run **Modify files** on the *Sample notes* cell: it reads the notes, logs a "
                       "plan, writes edited copies and opens a CHANGES.md showing each diff."),
    (("terminal", "shell"), "**Terminal session** plays a clone and a test run in the live terminal. "
                            "You can type into it — it echoes and politely refuses to execute."),
    (("install", "download", "desktop"), "The real app runs on your machine as a desktop app or a local "
                                         "server; the landing page has the download links."),
    (("hello", "hi", "hey"), "Hello! I am a scripted stand-in for an assistant. Ask about the demo, "
                             "the terminal or editing files."),
    (("thank", "thanks"), "You are welcome. Everything here resets when you reload the page."),
)

DEFAULT_REPLY = (
    "This is a demo: I answer from a small table of canned replies, so I "
    "cannot follow that. A real run would send your message to the model "
    "account you configured. Try asking about *files*, the *terminal*, or "
    "what is *real* here."
)


def pick_reply(message: str) -> str:
    words = set(re.findall(r"[a-z]+", message.lower()))
    for keys, reply in SCRIPTED_REPLIES:
        if words.intersection(keys):
            return reply
    return DEFAULT_REPLY


def cell_name_for(demo, fresh: str) -> str:
    """The target cell's own name when it already holds files (a run on an
    existing cell must not rename it), else ``fresh`` — an empty cell is
    named after what the run puts in it."""
    if demo.inputs and demo.inputs[0].get("files") and demo.inputs[0].get("name"):
        return str(demo.inputs[0]["name"])
    return fresh


def tokens(text: str) -> list[str]:
    """Word-sized streaming chunks; each keeps its trailing whitespace."""
    out: list[str] = []
    word = ""
    for ch in text:
        word += ch
        if ch in " \n":
            out.append(word)
            word = ""
    if word:
        out.append(word)
    return out


def pause_after(chunk: str) -> float:
    """Deterministic pacing: a beat per word, longer at punctuation and lines."""
    stripped = chunk.strip()
    if chunk.endswith("\n"):
        return 0.28
    if stripped.endswith((".", "!", "?", ":")):
        return 0.22
    if stripped.endswith(","):
        return 0.12
    return 0.05 + 0.006 * len(stripped)


class Conversation:
    def __init__(self, demo, session, factor: float):
        self.demo = demo
        self.session = session
        self.factor = factor
        self.messages: list[dict] = []

    def transcript(self) -> str:
        return json.dumps({"messages": self.messages}, indent=2, ensure_ascii=False) + "\n"

    async def write_transcript(self) -> None:
        self.demo.update_ui_file(self.session.transcript, self.transcript())
        await self.demo.commit()

    async def user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    async def reply(self, content: str, thinking: str = "") -> None:
        demo, session, factor = self.demo, self.session, self.factor
        await session.start_turn()
        if thinking:
            for chunk in tokens(thinking):
                await session.thinking(chunk)
                await demo.sleep(0.045 * factor)
            await demo.sleep(0.5 * factor)
        for chunk in tokens(content):
            await session.delta(chunk)
            await demo.sleep(pause_after(chunk) * factor)
        entry = {"role": "assistant", "content": content}
        if thinking:
            entry["thinking"] = thinking
        self.messages.append(entry)
        # The transcript must be on disk before turn_end: the renderer
        # reconciles the bubble it streamed against the durable file.
        await self.write_transcript()
        await session.end_turn()


async def play(demo) -> None:
    factor = PACE.get(str(demo.params.get("pace", "normal")), 1.0)
    show_thinking = bool(demo.params.get("show_thinking", True))
    idle_timeout = float(demo.params.get("idle_timeout_seconds", 20) or 20)
    opening = (demo.prompt or "").strip() or OPENING

    demo.phase(SPINNING_UP, "Opening the chat")
    demo.log("Demo chat: replies are scripted — no model is called.")
    session = demo.chat_session("Demo chat")
    conversation = Conversation(demo, session, factor)
    demo.update_ui_file(session.transcript, conversation.transcript())
    # A run on a cell that holds files keeps that cell's name (the files
    # stay too); an empty cell is named after the chat.
    await demo.commit(cell_name=cell_name_for(demo, "Demo chat"))
    await demo.reveal_file(session.pointer_path)
    await demo.sleep(0.8 * factor)

    demo.phase(RUNNING, "Replying to the opening message")
    await conversation.user(opening)
    await conversation.write_transcript()
    await demo.sleep(0.6 * factor)
    await conversation.reply(FIRST_REPLY, THINKING if show_thinking else "")
    demo.log("First scripted turn done.")

    await demo.sleep(1.4 * factor)
    demo.phase(RUNNING, "Playing the follow-up question")
    await conversation.user(FOLLOW_UP)
    await conversation.write_transcript()
    await demo.sleep(0.7 * factor)
    await conversation.reply(SECOND_REPLY)
    demo.log("Second scripted turn done. Waiting for your messages.")

    demo.phase(AWAITING_INPUT, "Send a message — the replies are canned")
    while True:
        try:
            message = await asyncio.wait_for(session.wait_user_message(), timeout=idle_timeout)
        except asyncio.TimeoutError:
            demo.log(f"No message for {idle_timeout:.0f}s — ending the demo chat.")
            break
        if message is None:
            break
        demo.phase(RUNNING, "Replying")
        await conversation.user(message)
        await conversation.write_transcript()
        await demo.sleep(0.5 * factor)
        await conversation.reply(pick_reply(message))
        demo.phase(AWAITING_INPUT, "Send a message — the replies are canned")

    demo.phase(FINISHING, "Saving the transcript")
    await conversation.write_transcript()
    demo.finish(success=True, message="Scripted conversation finished")
