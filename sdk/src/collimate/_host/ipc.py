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
    # The workspace paths the live-files mirror skipped (neither way), from
    # its final reconcile: a repository's tracked file among them stays out
    # of the deletions the host's git harvest records
    "mirror_skipped": OpSpec("request"),
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
    # The run's live options: the set it offers (normalize_run_options) and
    # the changes the user makes to them, oldest first
    "publish_options": OpSpec("notify"),
    "poll_option_change": OpSpec("request"),
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
    # The keystore's accepted structures, no values: every run may ask
    "keystore_structures": OpSpec("request"),
    # The run's annotation context
    **{op: OpSpec("request") for op in ANNOTATION_OPS},
}

#: A run option's ``type``: a choice among values, or an on/off switch
#: (ACP's SessionConfigOption types).
RUN_OPTION_TYPES: frozenset = frozenset({"select", "boolean"})

#: The most options one published set holds, and the most values one select
#: offers.
RUN_OPTIONS_MAX: int = 64
RUN_OPTION_VALUES_MAX: int = 1000

#: The option changes the host holds for a run until it polls them; past
#: this the oldest is dropped.
RUN_OPTION_CHANGES_MAX: int = 32


def _optional_text(entry: dict, key: str, where: str) -> dict:
    value = entry.get(key)
    if value is None:
        return {}
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string")
    return {key: value} if value else {}


def _select_values(values, where: str) -> list:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f"{where}: a select needs its options")
    if len(values) > RUN_OPTION_VALUES_MAX:
        raise ValueError(f"{where}: at most {RUN_OPTION_VALUES_MAX} values")
    out = []
    for value in values:
        if (not isinstance(value, dict) or not isinstance(value.get("value"), str)
                or not value["value"] or not isinstance(value.get("name"), str)):
            raise ValueError(f"{where}: each value is {{value, name, description?}}")
        out.append({"value": value["value"], "name": value["name"] or value["value"],
                    **_optional_text(value, "description", where)})
    return out


def normalize_run_options(options) -> list:
    """``options`` as the run-option wire carries it: a list of
    ``{id, name, description?, category?, type, currentValue, options?}``
    (ACP's SessionConfigOption shape). ``type`` is ``select`` (a string
    ``currentValue`` and ``options``, ``[{value, name, description?}]``)
    or ``boolean`` (a bool ``currentValue`` and no ``options``). Ids are
    unique; other keys are dropped. Raises ``ValueError`` naming the first
    malformed entry."""
    if not isinstance(options, (list, tuple)):
        raise ValueError("run options must be a list")
    if len(options) > RUN_OPTIONS_MAX:
        raise ValueError(f"at most {RUN_OPTIONS_MAX} run options")
    out: list = []
    seen: set = set()
    for i, entry in enumerate(options):
        where = f"run option {i}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        oid, name, kind = entry.get("id"), entry.get("name"), entry.get("type")
        if not isinstance(oid, str) or not oid:
            raise ValueError(f"{where}: id must be a non-empty string")
        where = f"run option {oid!r}"
        if oid in seen:
            raise ValueError(f"{where}: the id is used twice")
        seen.add(oid)
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: name must be a non-empty string")
        if kind not in RUN_OPTION_TYPES:
            raise ValueError(f"{where}: type must be select or boolean")
        current = entry.get("currentValue")
        option = {"id": oid, "name": name, **_optional_text(entry, "description", where),
                  **_optional_text(entry, "category", where), "type": kind}
        if kind == "boolean":
            if not isinstance(current, bool):
                raise ValueError(f"{where}: a boolean's currentValue must be true or false")
            option["currentValue"] = current
        else:
            if not isinstance(current, str):
                raise ValueError(f"{where}: a select's currentValue must be a string")
            option["currentValue"] = current
            option["options"] = _select_values(entry.get("options"), where)
        out.append(option)
    return out


def option_change_error(options: list, option_id, value) -> str:
    """Why ``{id: option_id, value}`` is no change to the published set
    ``options`` (normalized): an id the set does not hold, a boolean's
    non-bool value, or a value its select does not offer. ``""`` when it is
    one."""
    option = next((o for o in options if o["id"] == option_id), None)
    if option is None:
        return f"the run offers no option {option_id!r}"
    if option["type"] == "boolean":
        return "" if isinstance(value, bool) else f"{option['name']} takes true or false"
    if isinstance(value, str) and any(v["value"] == value for v in option["options"]):
        return ""
    return f"{option['name']} offers no value {value!r}"


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



def writer_label(actor) -> str:
    """Who wrote a revision, in words, from a conflict's ``current_writer``
    (``{"kind": "user" | "host" | "run", ...}``)."""
    kind = actor.get("kind") if isinstance(actor, dict) else None
    if kind == "user":
        return "the user"
    if kind == "host":
        return "the host"
    if kind == "run":
        generator = actor.get("generator")
        return f"run {actor.get('label')}" + (f" ({generator})" if generator else "")
    return "another writer"


def conflict_message(path: str, revision: int, writer: str, candidates) -> str:
    """The refusal text of a write of ``path`` that a newer ``revision``
    (saved by ``writer``, in words) refused: the run's own ``candidates``
    beside it, and the two ways on (keep that version, or replace it
    deliberately)."""
    candidates = ", ".join(candidates or ()) or "none"
    return (f"{path} was not written: {writer} saved a newer version "
            f"(revision {revision}) after this run last read it; this run's bytes are kept "
            f"beside it for review (candidates: {candidates}). Read revision {revision}: to accept "
            f"it, write its bytes back unchanged (FileConflict.accept()); to replace it deliberately, "
            f"write {path} again with replace_revision={revision}")


def refused_message(refused: list) -> str:
    """The :class:`PermissionError` text for the entries an ``ack_update``
    names in ``refused`` (ownership reason/owner, ``handler_invalid``/error,
    or the SDK's own ``conflict`` entries, which name the newer revision,
    its writer and the run's candidates: :func:`conflict_message`)."""
    parts = []
    for entry in refused:
        entry = entry if isinstance(entry, dict) else {"path": str(entry)}
        path = entry.get("path", "")
        if entry.get('reason') == 'handler_invalid':
            parts.append(f"{path}: {entry.get('error') or 'invalid file'}")
        elif entry.get('reason') == 'conflict' and entry.get('revision') is not None:
            parts.append(conflict_message(path, entry["revision"], writer_label(entry.get("writer")),
                                          entry.get("candidates")))
        elif entry.get('reason') == 'conflict':
            parts.append(entry.get('error') or f"{path} conflicts with another writer's save")
        elif entry.get("reason") == "host_owned":
            parts.append(f"{path} is the run card: the host owns it")
        elif entry.get("reason") == "ui_type":
            parts.append(f"{path} is not a UI file type the manifest declares "
                         "in ui_files")
        elif entry.get("reason") == "reserved":
            parts.append(f"{path} is reserved: a repository's .git and .collrun/ "
                         "are never cell files")
        else:
            owner = entry.get("owner") or "another run"
            parts.append(f"{path} is locked by {owner}")
    return "; ".join(parts) + " (the rest of the update was committed)"
