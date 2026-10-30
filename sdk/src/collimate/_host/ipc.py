# SPDX-License-Identifier: MIT
"""Wire constants shared by the SDK and the application host."""

import secrets
from dataclasses import dataclass
from typing import Literal

#: Maximum bytes in one newline-terminated JSON frame, including its newline.
IPC_MAX_LINE_BYTES: int = 10_000_000

#: The verdicts a ``finish`` frame's optional ``outcome`` may carry.
FINISH_OUTCOMES: frozenset = frozenset({"pass", "fail"})

#: The longest ``note`` a ``finish`` frame carries (the host's failure_reason cap).
FINISH_NOTE_MAX_CHARS: int = 300


def finish_note(text) -> "str | None":
    """``text`` as a ``finish`` frame's ``note``: one line (whitespace runs
    collapsed to one space), at most :data:`FINISH_NOTE_MAX_CHARS`
    characters (a longer one ends in ``…``); None when nothing is left."""
    if text is None:
        return None
    line = " ".join(str(text).split())
    if not line:
        return None
    if len(line) > FINISH_NOTE_MAX_CHARS:
        line = line[: FINISH_NOTE_MAX_CHARS - 1] + "…"
    return line


#: The annotation ops (generator → host). Each is a request answered by
#: ``ack_<op>``; a refusal is an ack with ``ok: false`` and an ``error`` code.
ANNOTATION_OPS: tuple = ("annotations_poll", "annotations_pull", "annotations_post",
                         "annotations_list")

#: The grant capability of the host's environment plane (``docker: full``):
#: placing a container, running in it, exposing its ports, a terminal in it.
CAP_ENV_PLANE: str = "env_plane"


@dataclass(frozen=True, slots=True)
class OpSpec:
    """How one IPC op travels. A ``request`` carries a ``req_id`` and its
    caller waits for the ack (``ack_<op>``; ``update_cell``'s is
    ``ack_update``) or a ``nak``; a ``notify`` is never answered.
    ``capability`` is what the host's grant gate requires before the handler
    runs; None is ungated."""
    kind: Literal["request", "notify"]
    capability: str | None = None


#: Every IPC op. All of them go generator → host: the host only answers a
#: request (ack or ``nak``) and sends nothing unsolicited. The host builds
#: its dispatch table (``_op_<name>`` per op), its request set and its
#: capability gate from this table; adding an op is a line here, the
#: ``_host/bridge.py`` call that sends it and the app's ``_op_<name>`` handler.
OPS: dict[str, OpSpec] = {
    # The run's cell
    "update_cell": OpSpec("request"),
    "read_cell_files": OpSpec("request"),
    "read_cell_hashes": OpSpec("request"),
    "reveal_file": OpSpec("notify"),
    # Lifecycle
    "phase": OpSpec("notify"),
    "progress": OpSpec("notify"),
    "finish": OpSpec("notify"),
    # File handlers
    "list_handlers": OpSpec("request"),
    "handler_skills": OpSpec("request"),
    "validate_file": OpSpec("request"),
    # Host services
    "client_caps": OpSpec("request"),
    "surface_register": OpSpec("request"),
    "chat_poll": OpSpec("request"),
    "stream_event": OpSpec("notify"),
    "poll_renderer_action": OpSpec("request"),
    "port_bound": OpSpec("request"),
    # Terminals: in one of the run's containers (the environment plane), or
    # on this machine. The host form is gated by its handler: the run's
    # grant must allow host execution (docker none or full, commands not
    # deny) and its harness placement, where it has one, must put the CLI on
    # this machine — facts no single capability names.
    "create_terminal_session": OpSpec("request", CAP_ENV_PLANE),
    "create_host_terminal_session": OpSpec("request"),
    "terminal_status": OpSpec("request"),
    "destroy_terminal_session": OpSpec("notify"),
    # The environment plane
    "env_place": OpSpec("request", CAP_ENV_PLANE),
    "env_exec": OpSpec("request", CAP_ENV_PLANE),
    "env_expose_port": OpSpec("request", CAP_ENV_PLANE),
    "env_close": OpSpec("request", CAP_ENV_PLANE),
    # Accounts
    "account": OpSpec("request"),
    "commit_account": OpSpec("request"),
    "account_materialize": OpSpec("request"),
    "account_release": OpSpec("request"),
    # The run's annotation context
    **{op: OpSpec("request") for op in ANNOTATION_OPS},
}

#: How an annotation message reaches its run: in queue order, or ahead of the
#: queue, cutting the current turn short.
ANNOTATION_MODES: frozenset = frozenset({"queued", "interrupt"})

#: Who wrote an annotation message: the user, or a run (tagged with its label).
ANNOTATION_AUTHORS: frozenset = frozenset({"user", "run"})

#: An annotation message's kind: a message, or the run's prompt the host
#: seeds into its context (born read).
ANNOTATION_KINDS: frozenset = frozenset({"message", "prompt"})

#: The longest annotation body, in UTF-8 bytes (the host answers
#: ``too_large`` above it).
ANNOTATION_BODY_MAX_BYTES: int = 64 * 1024

#: An annotation message id: ``a_`` + 16 lowercase hex digits, minted by the
#: host for a user's message and by the SDK for a run's post.
ANNOTATION_ID_PATTERN: str = r"a_[0-9a-f]{16}"


def new_annotation_id() -> str:
    """A fresh annotation message id (:data:`ANNOTATION_ID_PATTERN`)."""
    return "a_" + secrets.token_hex(8)


#: The run-end Logs artifact the host writes from the run's annotation context.
ANNOTATIONS_LOG: str = "annotations.json"

#: Log paths the host writes itself: a run's ``category="log"`` write to one
#: is refused.
RESERVED_LOG_NAMES: frozenset = frozenset({"logs.txt", ANNOTATIONS_LOG})


# What ``client_caps`` reports with no host on the other end. Kept in the
# host's flat snake_case shape so a generator run offline reads the same
# keys it reads in the app — only the values differ.
DEV_CLIENT_CAPS: dict = {
    "client": "dev",
    "native_guests": False,
    "guest_capture": False,
    "persistent_partitions": False,
    "direct_loopback": False,
    "external_open": False,
    "custom_title_bar": False,
    "custom_window_controls": False,
}



def refused_message(refused: list) -> str:
    """The :class:`PermissionError` text for the entries an ``ack_update``
    names in ``refused`` (ownership reason/owner or ``handler_invalid``/error)."""
    parts = []
    for entry in refused:
        entry = entry if isinstance(entry, dict) else {"path": str(entry)}
        path = entry.get("path", "")
        if entry.get('reason') == 'handler_invalid':
            parts.append(f"{path}: {entry.get('error') or 'invalid file'}")
        elif entry.get("reason") == "host_owned":
            parts.append(f"{path} is the run card: the host owns it")
        elif entry.get("reason") == "ui_type":
            parts.append(f"{path} is not a UI file type the manifest declares "
                         "in ui_files")
        else:
            owner = entry.get("owner") or "another run"
            parts.append(f"{path} is locked by {owner}")
    return "; ".join(parts) + " (the rest of the update was committed)"
