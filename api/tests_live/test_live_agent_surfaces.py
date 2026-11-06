# SPDX-License-Identifier: MIT
"""The live product-path suite for the Agent cell's surfaces (agent-v2
§9.1.7).

Each test drives ONE scenario end to end against a real server, a real
browser and a real harness — the matrix that was previously verified by hand
(a terminal run per harness, the model the bound account names, a posted
annotation read and answered in its thread, the attachment path). The Agent
cell holds one generator, ``agent``, whose ``surface`` setting picks
annotation, chat or the terminal (CLI). An interactive run ends by its Stop
and nothing starts by itself: the next run is a runner the user starts.

Reading the assertions
----------------------
The contracts under test are agent-v2 §9.1, and the generator a seeded
channel runs is the bundled one, the checkout's ``content/generators/``.
Every assertion here states the contract as it stands, so a red run means
the shipped generator no longer keeps it.

How a scenario starts a run
---------------------------
Every scenario binds a model account to a runner built from one of the
generator's shipped configs and starts THAT — a run carries a runner id, its
revision and the cell, and nothing else. The model, the endpoint and the
harness are facts of the bound account; the surface a session opens in is the
runner's ``surface`` setting. An interactive session seeds no opening message, so a
scenario that needs the agent to do something types it in the composer once
the session is idle.

House rules for adding a scenario:

* its own cell — a wedged session must never reach the next test;
* bounded — every wait has a timeout and a label that says what was awaited;
* product path — click the status pane and type in the composer; reach for the
  API only where the browser genuinely cannot (a message's exact read state
  and thread, which the status pane draws but does not expose as data).
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

import pytest

from .conftest import (
    CHAT_TRANSCRIPT,
    CLAUDE_MODEL,
    CHAT_POINTER,
    SESSION_RECORD,
    TERMINAL_TRANSCRIPT,
    annotation_context,
    post_annotation,
    wait_reply,
)

pytestmark = pytest.mark.live

NOTES = {"name": "notes.md",
         "content": "# Zephyr Ledger Notes\n\n"
                    "A small file for the agent to read.\n"
                    "It records two entries and nothing else.\n"}


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════

def open_cell(cell_view, cell_id: str):
    """This cell's view, on a freshly loaded page: the load's active-run
    recovery attaches the run the API started, so the run light and the
    status pane know it from the start.
    """
    return cell_view(cell_id).reopen()


def assistant_replies(messages: list[dict]) -> list[dict]:
    return [m for m in messages
            if m.get("role") == "assistant" and (m.get("content") or "").strip()]


def claude_records(api, cell_id: str, run_id: str) -> list[tuple[str, str, str]]:
    """``(role, model, text)`` from claude's OWN session jsonl.

    The transcript is what Collimate saved; this is what the harness saw —
    the only place the answering model is recorded, so it is how a run is
    shown to have used the model its bound account names.

    A run's harness home is a disposable temp dir (it holds credentials);
    the nonsecret session state is copied out to
    ``<cache>/collimate/agent-state/<cell id>/claude-code`` when the harness
    closes (``acp_client``'s ``close_harness_homes``). Read it AFTER the run
    has finished — mid-run there is nothing there. The cache is keyed by the
    placeholder cell id, so the workspace slug only distinguishes runs WITHIN
    one cell; we fall back to every project dir when it matches nothing,
    since the harness is free to name that directory something else. The
    workspace comes from the run's ACP session record, a run log artifact.
    """
    session = api.run_log(run_id, SESSION_RECORD)
    assert isinstance(session, dict), (
        f"the run logged no {SESSION_RECORD} — the ACP session never opened")
    workspace = session["cwd"]
    slug = "".join("-" if c in "/._" else c for c in workspace)
    home = os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
        "collimate", "agent-state", cell_id, "claude-code", "projects",
    )
    paths = sorted(glob.glob(os.path.join(home, f"{slug}*", "*.jsonl")))
    if not paths:
        paths = sorted(glob.glob(os.path.join(home, "*", "*.jsonl")))
    rows: list[tuple[str, str, str]] = []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                msg = event.get("message") or {}
                content = msg.get("content")
                text = (content if isinstance(content, str) else " ".join(
                    p.get("text", "") for p in (content or [])
                    if isinstance(p, dict) and p.get("type") == "text"))
                if not text.strip():
                    continue
                if event.get("type") == "assistant":
                    rows.append(("assistant", msg.get("model") or "", text))
                elif event.get("type") == "user":
                    rows.append(("user", "", text))
    return rows


# ══════════════════════════════════════════════════════════════════════════
# 1. claude · chat — reasoning record, the account's model, mid-stream send
# ══════════════════════════════════════════════════════════════════════════

def test_claude_chat_reasoning_block_reads_as_prose(
        claude_code, api, cell_view, runners):
    """§9.1 (the polish round's trace-chunk contract), and the model the run
    actually answered on.

    A saved Reasoning block must read as prose — the per-chunk trace events
    used to splice a blank line into the middle of a sentence. The answering
    model is checked against claude's OWN session jsonl, because the model is
    a fact of the runner's bound account and no Collimate-side artifact can
    prove the harness honoured it.
    """
    runner = runners("claude-code", claude_code, "Reasoning live", surface="chat")
    cell = api.new_cell("live-claude-reasoning", [NOTES])
    run = api.start_runner(cell, runner)
    api.wait_idle(run, timeout=180)
    view = open_cell(cell_view, cell)
    view.send("Read notes.md and summarize it in one sentence. Think hard "
              "about whether the file name fits its content, then give your "
              "verdict.")
    api.wait_assistant(cell, 1, timeout=300, run_id=run)

    reply = assistant_replies(api.messages(cell))[0]
    thinking = (reply.get("thinking") or "").strip()
    assert thinking, (
        "the first turn saved no Reasoning block — the transcript's `thinking` "
        "is what §9.1's trace-chunk coalescing is about, so an empty one makes "
        "this scenario meaningless (check the harness still streams thinking)")
    mid_sentence = re.search(r"[a-z,;]\n\n[a-z]", thinking)
    assert not mid_sentence, (
        "the saved thinking has a blank line mid-sentence at "
        f"{thinking[max(0, mid_sentence.start() - 60):mid_sentence.end() + 60]!r} "
        "— streamed chunks must coalesce per contiguous block, and only BLOCKS "
        "join with a paragraph break")
    view.shot("answered")

    # The harness writes its session state out when it closes, so the run has
    # to be over before its records can be read.
    api.stop_run(run)
    api.wait_finished(run, timeout=180)
    answered = [model for role, model, _ in claude_records(api, cell, run)
                if role == "assistant" and model]
    assert answered, "claude's session jsonl recorded no answering model"
    assert all(CLAUDE_MODEL in model for model in answered), (
        f"the run answered on {answered!r}, but the runner's bound model "
        f"account names {CLAUDE_MODEL!r} — a run's model is the account's")


def test_claude_chat_message_sent_midstream_queues(
        claude_code, api, cell_view, runners):
    """§9.1.3 — a message sent while a turn streams QUEUES behind it: both
    replies land, and the first turn is not cut short."""
    runner = runners("claude-code", claude_code, "Queue live", surface="chat")
    cell = api.new_cell("live-claude-queue", [NOTES])
    run = api.start_runner(cell, runner)
    api.wait_idle(run, timeout=180)
    view = open_cell(cell_view, cell)
    view.open_chat_tab()

    base = api.n_assistant(cell)
    view.send("Write a numbered list of 30 short fun facts about rivers, "
              "one sentence each.")
    api.wait_busy(run, timeout=60)
    time.sleep(5)                       # let the first turn stream a while
    assert "Stop generating" in view.composer_buttons(), (
        "the composer is not in its streaming state — the second message would "
        "not be a mid-stream send at all")
    view.send("Never mind the rivers — reply with the single word PIVOT.")
    view.shot("second-message-sent")

    api.wait_idle(run, timeout=300)
    time.sleep(3)
    replies = assistant_replies(api.messages(cell))[base:]
    assert len(replies) >= 2, (
        f"the queued message did not run after the rivers turn; got "
        f"{len(replies)} repl(y|ies)")
    assert not replies[0].get("stopped"), (
        "the first reply is marked stopped — the second message redirected it "
        "instead of queueing")
    assert "PIVOT" in (replies[-1].get("content") or "").upper()
    api.stop_run(run)


# ══════════════════════════════════════════════════════════════════════════
# 2. claude · annotation — a posted message is read and answered in its thread
# ══════════════════════════════════════════════════════════════════════════

def test_claude_annotation_run_reads_a_posted_message_and_replies_in_its_thread(
        claude_code, api, cell_view, runners):
    """An interactive annotation run listens to its context: a message
    posted there is pulled (read) as a turn of its own and answered with a
    reply in that message's thread. The run opens no file and writes no
    chat files; its Stop seals the context, which the run's Logs keep as
    ``annotations.json``."""
    runner = runners("claude-code", claude_code, "Annotation live", surface="annotation")
    cell = api.new_cell("live-claude-annotation", [NOTES])
    run = api.start_runner(cell, runner)
    api.wait_until(lambda: annotation_context(api, cell, run)["listening"], timeout=300,
                   label="the run to listen to its annotation context", run_id=run)
    view = open_cell(cell_view, cell)
    root = post_annotation(api, cell, run,
                           "Read notes.md and reply with how many entries it records.")
    reply = wait_reply(api, cell, run, root, timeout=300)
    assert (reply.get("body") or "").strip(), "the run's reply is empty"
    assert reply["author_label"] == next(r["label"] for r in api.active_runs() if r["run_id"] == run)
    shown = {m["id"]: m for m in view.messages(run)}
    assert root in shown and shown[root]["read"], shown
    assert reply["id"] in shown, shown
    view.shot("answered")
    assert api.ui_path(cell, CHAT_POINTER) is None and api.ui_path(cell, CHAT_TRANSCRIPT) is None, (
        "an annotation run writes no chat files")

    view.click_stop(run)
    api.wait_finished(run, timeout=180)
    assert annotation_context(api, cell, run)["sealed"] is True
    assert "annotations.json" in api.run_log_paths(run), api.run_log_paths(run)


# ══════════════════════════════════════════════════════════════════════════
# 3. claude · the attachment path
# ══════════════════════════════════════════════════════════════════════════

def test_claude_composer_attachment_lands_in_the_cell(
        claude_code, api, cell_view, runners, tmp_path):
    """The attachment contract: a file attached in the composer becomes a
    CELL file at ``attachments/<name>`` and the model is pointed at that
    path — never a private ``_agent/attachments/<n>-<name>``."""
    source = tmp_path / "ledger-secret.txt"
    source.write_text("The secret ingredient is cardamom.\n", encoding="utf-8")

    runner = runners("claude-code", claude_code, "Attachment live", surface="chat")
    cell = api.new_cell("live-claude-attachment", [NOTES])
    run = api.start_runner(cell, runner)
    api.wait_idle(run, timeout=180)
    view = open_cell(cell_view, cell)
    view.open_chat_tab()
    view.attach(str(source))
    view.shot("attached")
    view.send("What is the secret ingredient in the attached file? Also tell "
              "me the exact relative path you read it from.")
    api.wait_assistant(cell, 1, timeout=300, run_id=run)

    expected = f"attachments/{source.name}"
    paths = api.cell_paths(cell)
    assert expected in paths, (
        f"the attachment did not land at {expected} — cell holds "
        f"{sorted(p for p in paths if 'attach' in p)}")

    messages = api.messages(cell)
    sent = [m for m in messages if m.get("role") == "user"][-1]
    assert sent.get("attachments"), "the user message records no attachment"
    reply = assistant_replies(messages)[-1]["content"]
    assert "cardamom" in reply.lower(), (
        f"the model did not read the attachment: {reply[:200]!r}")
    assert expected in reply, (
        f"the reply cites a different path than the cell's: {reply[:300]!r}")
    api.stop_run(run)


# ══════════════════════════════════════════════════════════════════════════
# 4/5. opencode · goose — a terminal run tailed live, then stopped (docker)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("backend,context_file", [
    ("opencode", "AGENTS.md"),
    ("goose", ".goosehints"),
])
def test_container_terminal_run_tails_its_transcript_and_stops(
        container_agent, api, cell_view, runners, backend, context_file):
    """A terminal (CLI) run on a containerized harness, tailed while it is
    live and ended by its Stop.

    The harness is the one the bound model entry names — the runner is the
    agent's shipped config for that backend on its terminal surface, and the
    OpenRouter account under the entry is what the container bills to.

    What the run has to keep (J2, §9.1.2/.4/.8):

    * the terminal turn is tailed into the run's terminal transcript DURING
      the TTY: the tail's own log lines, read off the event stream while the
      run is live, count the first prompt, its reply and the second prompt
      merged. The tail holds the record's last reply back until something
      follows it (a reply that has held still can still grow), so it is the
      second prompt that lets the first reply settle live; the leg's
      finalize adds the second reply. The transcript is a run log artifact
      once the run ends, and there the model's own MERIDIAN reply (an
      assistant message, not the prompt that names the word) must be;
    * the run's Stop (in its drilldown) ends the TTY and the run;
    * the terminal pointer is gone from the cell, and the harness context
      file never reaches it;
    * nothing starts by itself.
    """
    model = container_agent(backend)
    runner = runners(backend, model, f"{backend} terminal live", surface="terminal")
    cell = api.new_cell(f"live-{backend}-terminal", [NOTES])
    run = api.start_runner(cell, runner)
    pointer = api.run_path(run, f"terminal/{backend}.terminal")
    api.wait_until(
        lambda: pointer in api.cell_paths(cell),
        timeout=420, every=3, label="the terminal pointer to be written",
        run_id=run)

    # Readiness is STRUCTURAL: the TTY paints into a canvas, so the driver
    # can see that the terminal is mounted and laid out but never what it
    # says. Everything the model produced is asserted on the run's terminal
    # transcript. The page opens after the container is up (starting one can
    # abort an in-flight renderer fetch).
    view = open_cell(cell_view, cell)
    if not api.happens_within(view.terminal_mounted, timeout=90, every=3,
                              run_id=run):
        view.reopen()
        api.wait_until(view.terminal_mounted, timeout=180, every=3,
                       label=f"the {backend} TTY renderer to mount", run_id=run)
    time.sleep(15)                      # let the TUI finish its first draw
    view.shot("terminal-ready")

    # A deliberately CONTENT-FREE probe: the live check is "a terminal turn
    # reaches the transcript", so the prompt must not also depend on the
    # model's memory or its willingness (asking a small model for a
    # "codeword" earns a refusal often enough to make the test lie).
    view.type_in_terminal("Reply with exactly the word MERIDIAN and nothing else.")

    def messages_of(doc) -> list[dict]:
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                return []
        return doc.get("messages", []) if isinstance(doc, dict) else []

    def transcript_messages(run_id: str) -> list[dict]:
        return messages_of(api.run_log(run_id, TERMINAL_TRANSCRIPT))

    tail_line = re.compile(
        re.escape(TERMINAL_TRANSCRIPT) + r": (\d+) message\(s\) tailed live")

    def tailed_live() -> int:
        """Messages the tail reports merged so far, summed over its own log
        lines (``terminal_tail.TranscriptTail.merge`` writes one per appended
        batch) on the run's event stream while the run is live. The
        transcript it appends to is a LOG-category write
        (``gen.update_file(..., category="log")``), which reaches the host at
        the run's next flush — the run end — so mid-TTY the tail is observable
        only through these lines; what it wrote is asserted on the run log
        once the run has ended."""
        batches: dict = {}
        for i, event in enumerate(api.run_events(run).get("events", [])):
            found = tail_line.search(str(event))
            if found:
                key = event.get("id", i) if isinstance(event, dict) else i
                batches[key] = int(found.group(1))
        return sum(batches.values())

    # The reply stays the record's last message, held back by the tail,
    # until something follows it: a second prompt is what settles it live.
    # One: the first prompt; then give its one-word reply time to finish
    # before the second prompt goes in.
    api.happens_within(lambda: tailed_live() >= 1, timeout=180, every=3,
                       run_id=run)
    time.sleep(15)
    view.type_in_terminal("Now reply with exactly the word ZENITH and nothing else.")
    # Three: the first prompt, the MERIDIAN reply and the second prompt.
    api.happens_within(lambda: tailed_live() >= 3, timeout=180, every=3,
                       run_id=run)
    tailed = tailed_live()
    view.shot("terminal-answered")
    assert tailed >= 3, (
        f"the tail reported {tailed} message(s) merged into "
        f"{TERMINAL_TRANSCRIPT} during the TTY — the first prompt, its reply "
        "and the second prompt must be appended while the terminal is live, "
        "the reply once the second prompt follows it")
    # The second reply is held back until the leg's finalize; let it finish
    # before the Stop ends the TTY.
    time.sleep(15)

    # ── the run's Stop: the TTY ends and the run with it ───────────────────
    view.click_stop(run)
    api.wait_finished(run, timeout=420)
    view.shot("stopped")

    logged = transcript_messages(run)
    replies = [m.get("content") or "" for m in logged
               if m.get("role") == "assistant"]
    assert any("MERIDIAN" in text.upper() for text in replies), (
        "the model's terminal reply never reached the run's transcript: "
        f"{[(m.get('role'), (m.get('content') or '')[:60]) for m in logged]}")
    assert len(logged) >= tailed, (
        f"the run's transcript holds {len(logged)} message(s), fewer than the "
        f"{tailed} the tail reported merging live")
    paths = api.cell_paths(cell)
    assert pointer not in paths, "the terminal pointer is still in the cell after the run"
    assert TERMINAL_TRANSCRIPT not in paths, (
        "the terminal transcript is a run log artifact, never a cell file")
    assert context_file not in paths, (
        f"{context_file} reached the cell — the harness context file stays in "
        "its container")

    assert not api.happens_within(lambda: api.active_run_on(cell, besides=run),
                                  timeout=20, every=2), (
        "a run started on the cell by itself after the Stop")
