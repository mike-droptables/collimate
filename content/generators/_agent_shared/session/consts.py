# SPDX-License-Identifier: MIT
"""The agent session's shared vocabulary: turn sources, the run's surface
paths (:class:`SessionPaths`), the directives the session injects and the
timing constants — one place the session's modules (``entry`` / ``state``
/ ``annotation`` / ``corrections`` / ``terminal`` / ``run_end``) all import
from."""
from __future__ import annotations

import gen
from collimate.pointers import run_card_path


class Sources:
    """``ChatTurn.source`` values the session uses to know WHO started a
    turn (the composer, the selected file, a one-shot's opening, a batch of
    annotation messages, a host-refusal correction) — every branch that must
    treat a programmatic turn differently keys on these, never on prompt
    text."""
    COMPOSER = ""
    SELECTED_FILE = "@selected-file"      # the selected file, a chat's first message
    OPENING = "@opening"                  # a one-shot's opening turn
    ANNOTATIONS = "@annotations"          # a batch of annotation messages
    CORRECTION = "@correction"            # a host-refusal correction (not on terminal)


# The transcript's stand-in for a turn that produced no prose and no files —
# display text, never an answer.
EMPTY_REPLY = "_(empty response)_"


class SessionPaths:
    """The run's surface paths, all in its run folder: the chat session's
    pointer (a chat run's ``.chat`` surface, and every run's inbox
    registration path) and transcripts, the terminal pointer and the run
    card (always the host's). A chat or terminal run's surface file is the
    one the manifest's ``opens`` resolves to (``cfg["opens"]``, from
    ``session.surface_opens``); an annotation run opens none.
    """

    def __init__(self, *, label: str, folder: str, backend: str) -> None:
        self.label = label
        self.folder = folder
        self.chat_pointer = f"{folder}/chat/conversation.chat"
        self.chat_threads = f"{folder}/chat"
        self.terminal_pointer = f"{folder}/terminal/{backend}.terminal"
        self.run_card = run_card_path(label)

    @classmethod
    def for_run(cls, backend: str, *, cfg: dict | None = None) -> "SessionPaths":
        """The paths of the run ``gen.run_info()`` describes. With ``cfg``,
        a chat or terminal surface's file is the first of ``cfg["opens"]``,
        the manifest's declaration resolved by ``session.run``; the
        annotation surface opens none."""
        info = gen.run_info()
        paths = cls(label=info.label, folder=info.folder, backend=backend)
        if cfg is None:
            return paths
        surface = cfg["surface"]
        opens = cfg["opens"]
        if surface == "annotation":
            if opens:
                raise ValueError("the annotation surface opens no file")
            return paths
        if not opens:
            raise ValueError(f"the generator's opens names no file for the {surface} surface")
        if surface == "chat":
            paths.chat_pointer = opens[0]
        else:
            paths.terminal_pointer = opens[0]
        return paths


def identity_directive(paths: SessionPaths) -> str:
    """Every run's line on who it is among the runs of its cell (appended to
    every turn's system prompt and to a terminal's), with the pointer to the
    llm-summary skill every surface stages."""
    return (
        f"You are run {paths.label}. Everything under _runs/ that is not "
        f"your own folder ({paths.folder}/) is read-only to you, and your own "
        "folder holds only your interface files: every file you write goes "
        "at the workspace root or elsewhere outside _runs/. When the user "
        "asks for a summary or a handoff of the work, or to pick up from "
        "one, follow the llm-summary skill. The paths in these instructions "
        "override any path a skill names."
    )


def annotation_directive(*, oneshot: bool) -> str:
    """The Annotation surface's standing instructions (appended to every
    turn's system prompt there); a one-shot's adds how it ends."""
    text = (
        "This run works through annotations. The user does not see this "
        "conversation: they see the run's annotation context — the messages "
        "they post to you and your replies — and the files you change. Each "
        "@annotations turn carries their messages as <message> blocks (id, "
        "thread, author, mode) with what they point at: <context> items they "
        "picked in a file and <file> blocks for the cell files they link. "
        "Read what a message points at before you act. Reply with the "
        "annotate tool — annotate(text, reply_to=<thread>) — a short "
        "acknowledgement before long work and the outcome when it is done; "
        "a final answer you did not post yourself is posted for you as a "
        "reply to the last thread. Work output goes in the cell's files "
        "outside _runs/; for a visual answer write a self-contained .html "
        "file in the cell (inline styles, cell-relative links) and link it "
        "in your reply. A newer message may interrupt a turn: the next turn "
        "presents its messages again, so check any file the cut turn was "
        "writing. The annotations skill has the details."
    )
    if oneshot:
        text += (
            " This is a one-shot run: its opening message is the whole task. "
            "Complete it in that turn and reply to it; the run ends once the "
            "turn and any host corrections settle."
        )
    return text


# How long the run end waits for the surface and config loops to unwind — a
# terminal runner's export included — before clearing the live files: the
# host's stop grace is 15 s, and the run end must land inside it.
RUN_END_TASK_WAIT_S = 8.0
