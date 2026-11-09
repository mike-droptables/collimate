# SPDX-License-Identifier: MIT
"""Shared, dependency-light result/spec types for running a generator.

This is a **leaf** module: it imports nothing from :mod:`collimate.native`
or :mod:`collimate.testing` (only the wire constants of :mod:`collimate._host.ipc`),
so both can import it without a cycle. It defines the result returned by the
host-free harness (:func:`collimate.testing.run_generator`) and the messages
of a run's annotation context (``gen.annotations``). Public JSON file codecs
live in :mod:`collimate.wire`.

Re-exported (same object identity) from :mod:`collimate.native` (so
``gen.GeneratorResult`` resolves) and from :mod:`collimate.testing`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TypedDict, NotRequired

from collimate._host import ipc as _ipc


# ---------------------------------------------------------------------------
# Result / spec dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """A live endpoint a (daemon) run exposed — a bound port or a terminal."""

    kind: str  # "http" | "terminal"
    name: str = ""
    url: Optional[str] = None
    port: Optional[int] = None
    path: str = ""
    terminal_id: Optional[str] = None


@dataclass(frozen=True)
class RunInfo:
    """Who a run is to the host (:func:`gen.run_info`).

    ``label`` is the run's name among the runs of its cell —
    ``<runner_id>-<run_id[:8]>`` — and ``folder`` its run folder, ``_runs/<label>``: every file in
    it is the run's locked file."""

    run_id: str = ""
    channel: str = ""
    cell_id: str = ""  # the cell the run writes in; "" in a dev run
    runner_id: str = ""  # the saved runner's id; "" for a dev run
    runner_name: str = ""  # the saved runner's display name; "" likewise
    label: str = ""
    folder: str = ""


@dataclass(frozen=True)
class ContextItem:
    """What a renderer said about the user's selection when the message was
    written: the cell file it came from and the renderer that answered (both
    stamped by the host), the renderer's one-line ``label``, its ``text`` for
    the model, and its ``anchor`` locator (or None)."""

    path: str
    renderer_id: str
    label: str
    text: str
    anchor: Optional[dict] = None


@dataclass(frozen=True)
class Annotation:
    """One message of the run's annotation context (``gen.annotations``).

    ``text`` is markdown. ``author`` is ``"user"`` or ``"run"`` (a run's post,
    tagged with its ``author_label``); ``kind`` is ``"message"``, or
    ``"prompt"`` for the run's prompt the host seeds, already read.
    ``reply_to`` names the top-level message a reply answers (one level
    deep). ``mode`` is ``"queued"`` or ``"interrupt"``. The times are UTC
    ``YYYY-MM-DD HH:MM:SS``; ``read_at`` is None until a pull reads it.
    ``context`` holds the :class:`ContextItem` values the user attached and
    ``refs`` the cell paths the text links."""

    id: str
    text: str
    author: str
    author_label: Optional[str]
    kind: str
    reply_to: Optional[str]
    mode: str
    created_at: str
    read_at: Optional[str]
    context: tuple = ()  # tuple[ContextItem, ...]
    refs: tuple = ()  # tuple[str, ...]


@dataclass(frozen=True)
class AnnotationNotice:
    """What the last poll of the annotation context saw: its revision, the
    messages waiting to be pulled, and the interrupt counter."""

    rev: int
    unread: int
    interrupt_seq: int


def _wire_text(message: dict, key: str, *, optional: bool = False,
               nonempty: bool = False):
    value = message.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"annotation {key} must be {'non-empty ' if nonempty else ''}text")
    return value


def _wire_choice(message: dict, key: str, choices: frozenset) -> str:
    value = message.get(key)
    if value not in choices:
        raise ValueError(f"annotation {key} {value!r} is not one of {sorted(choices)}")
    return value


def _context_item_from_wire(item) -> ContextItem:
    if not isinstance(item, dict):
        raise ValueError("annotation context_items entries must be mappings")
    for key in ("path", "renderer_id", "label", "text"):
        if not isinstance(item.get(key), str):
            raise ValueError(f"annotation context item {key} must be text")
    anchor = item.get("anchor")
    if anchor is not None and not isinstance(anchor, dict):
        raise ValueError("annotation context item anchor must be a mapping or null")
    return ContextItem(path=item["path"], renderer_id=item["renderer_id"],
                       label=item["label"], text=item["text"],
                       anchor=dict(anchor) if anchor is not None else None)


def annotation_from_wire(message) -> Annotation:
    """A host annotation message (the wire ``Message``) as an
    :class:`Annotation`; :class:`ValueError` naming what is malformed. Keys
    the generator has no use for (``context_id``, ``position``, ``held``,
    ``revision``, ``edited_at``) are ignored."""
    if not isinstance(message, dict):
        raise ValueError("an annotation must be a mapping")
    items = message.get("context_items", [])
    if not isinstance(items, list):
        raise ValueError("annotation context_items must be a list")
    refs = message.get("refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        raise ValueError("annotation refs must be a list of paths")
    return Annotation(
        id=_wire_text(message, "id", nonempty=True),
        text=_wire_text(message, "body"),
        author=_wire_choice(message, "author", _ipc.ANNOTATION_AUTHORS),
        author_label=_wire_text(message, "author_label", optional=True),
        kind=_wire_choice(message, "kind", _ipc.ANNOTATION_KINDS),
        reply_to=_wire_text(message, "parent_id", optional=True, nonempty=True),
        mode=_wire_choice(message, "mode", _ipc.ANNOTATION_MODES),
        created_at=_wire_text(message, "created_at"),
        read_at=_wire_text(message, "read_at", optional=True),
        context=tuple(_context_item_from_wire(item) for item in items),
        refs=tuple(refs),
    )


@dataclass(frozen=True)
class GeneratorResult:
    """What a generator run produced.

    Returned by the host-free harness (:func:`collimate.testing.run_generator`)."""

    ok: bool
    files: dict = field(default_factory=dict)  # final output-cell files (the "result")
    log_files: dict = field(default_factory=dict)  # category='log' artifacts (run-scoped,
    # never part of the output cell; host-free harness only)
    name: Optional[str] = None  # last commit()/final cell name
    endpoints: list = field(default_factory=list)  # ports/terminals bound (daemons)
    tier_b_calls: list = field(default_factory=list)  # recorded serve_port/terminal/...
    # (host-free harness only)
    logs: list = field(default_factory=list)  # gen.log lines
    phases: tuple = ()  # every (phase, message) gen.phase set, in order
    error: Optional[str] = None  # exception message, None if ok
    traceback: Optional[str] = None  # full traceback when not ok
    note: Optional[str] = None  # the one-line note gen.finish recorded, else None
    outcome: Optional[str] = None  # "pass" / "fail" from gen.finish, else None
    annotations: tuple = ()  # the run's annotation context at its end (host-free harness only)

    def raise_for_status(self) -> None:
        """Raise ``RuntimeError`` when the run failed (``ok`` is False)."""
        if not self.ok:
            msg = self.error or "generator run failed"
            if self.traceback:
                msg = f"{msg}\n{self.traceback}"
            raise RuntimeError(msg)

    def output_text(self, path: str) -> str:
        """Read the declared text result path; empty when it is absent."""
        value = self.files.get(path, "")
        return value if isinstance(value, str) else ""

    def structured(self, path: str, model=None):
        """Parse the declared structured result path.

        Returns ``None`` when the file is absent or is not valid JSON — the
        common "no structured output" case the caller checks for. With
        ``model`` (a pydantic model class, or any callable taking keyword
        fields), the parsed object is validated as ``model(**parsed)`` and the
        instance returned; a present-but-wrong-shape output raises an
        informative error (the model's own validation error for an object with
        the wrong fields, or a ``ValueError`` for JSON that isn't an object),
        NOT ``None`` — so the caller can tell "no output" from "bad output".
        """
        import json as _json
        raw = self.files.get(path)
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            data = _json.loads(raw)
        except ValueError:
            return None
        if model is None:
            return data
        if not isinstance(data, dict):
            raise ValueError(
                f"structured output for {getattr(model, '__name__', model)} must be a "
                f"JSON object, got {type(data).__name__}")
        return model(**data)


@dataclass(frozen=True)
class ConfigField:
    """One field in a generator's settings schema."""

    key: str
    type: str = "text"  # text|textarea|number|dropdown|checkbox|list|object
    label: str = ""
    description: str = ""
    required: bool = False
    default: object = None  # resolved default (field default ⊕ ``values:`` block)
    options: list = field(default_factory=list)  # dropdown choices (else [])
    min: object = None  # number lower bound (else None)
    max: object = None  # number upper bound (else None)
    advanced: bool = False
    secret: bool = False  # masked input (an API key / token field)
    show_if: object = None  # visibility condition; None = always
    required_if: object = None  # conditional-required FieldMap (else None)


@dataclass(frozen=True)
class DraftFileSchema:
    """The field schema of one ``.set_yaml`` draft file a generator declares."""

    path: str  # e.g. "agent-config.set_yaml"
    fields: list = field(default_factory=list)  # list[ConfigField]


@dataclass(frozen=True)
class ConfigValidation:
    """The result of a configuration validation.

    ``per_file`` maps each target ``.set_yaml`` rel to the raw
    ``collimate.config_values.validate_config`` report (``ok``, ``visible``,
    ``effective_required``, ``missing_required``, ``invalid_options``,
    ``field_errors``) — inspect it for the advisory ``field_errors`` a caller
    may want to surface without blocking. ``ok`` is ``True`` iff every file's
    report is ``ok`` (no visibly-required-empty field, no disabled-option
    selection). ``errors`` is the flat, human-readable list of *blocking*
    violations across all files — non-empty exactly when ``ok`` is ``False``."""

    ok: bool
    per_file: dict = field(default_factory=dict)  # rel -> config_values report
    errors: list = field(default_factory=list)    # flat blocking-violation strings

    def raise_for_status(self) -> None:
        """Raise ``ValueError`` (joined ``errors``) when the config is invalid."""
        if not self.ok:
            raise ValueError("; ".join(self.errors) or "config validation failed")

    def __str__(self) -> str:
        if self.ok:
            return "config ok"
        return "config invalid: " + "; ".join(self.errors)


__all__ = [
    "Annotation",
    "AnnotationNotice",
    "ConfigField",
    "ConfigValidation",
    "ContextItem",
    "DraftFileSchema",
    "Endpoint",
    "GeneratorResult",
    "RunInfo",
    "annotation_from_wire",
]


class HandlerType(TypedDict):
    ext: str
    render_as: NotRequired[str | None]


class HandlerInfo(TypedDict):
    id: str
    name: str
    description: str
    types: list[HandlerType]


class ValidationResult(TypedDict):
    valid: bool
    error: NotRequired[str]
