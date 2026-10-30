# SPDX-License-Identifier: MIT
"""``RecordingBridge`` — a :class:`~collimate._host.bridge.HostBridge` subclass that
runs a generator **in-process** with no IPC peer, capturing what the run
emits instead of sending it to a host.

Subclass (not compose): ``fs_*`` / ``config`` / ``snapshot_workspace`` are
exactly the behaviour we want unchanged, so a subclass inherits them
verbatim and stays in sync if ``_host/bridge.py`` changes. Only the IPC-dependent
methods are overridden, to *record* into capture lists (the chat surface —
``surface_register`` / ``chat_poll`` / ``stream_event`` — is overridden to
drive a scripted inbound queue so host-free ``gen.serve_chat`` tests work, and
the annotation ops are answered from an in-memory annotation context that
orders, places and marks messages read as the host does).
Constructed via :meth:`RecordingBridge.create` (which skips
``HostBridge.__init__``'s socket/signal setup), so it never opens a socket
or installs a SIGTERM handler.

The bridge models the run's identity (run id, runner, label, run folder) and
the host's run-folder rules: a write or delete under another run's folder
(or of the run card) is refused, and ``foreign_runs`` seeds sibling runs'
folders so a test can exercise reading them. It models what
``gen.commit_account`` may change.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Optional

from collimate._host import ipc
from collimate._host.bridge import HostBridge, PartialRefusal, _inbox_declaration
from collimate._results import Annotation, ContextItem, Endpoint, annotation_from_wire
from collimate.pointers import RUNS_PREFIX, is_run_path, run_card_path, run_label


class _Unanswered:
    """A scripted approval answer: the human never answers this card."""

    def __repr__(self) -> str:
        return "UNANSWERED"


#: In an ``approvals`` script: leave this card unanswered, so it waits out
#: its timeout (or its caller's cancel).
UNANSWERED = _Unanswered()

# One pull takes whole units (a root and its unread replies) up to these
# bounds, as the host does; the first unit is always taken and the rest stay
# unread for the next pull.
_PULL_MAX_MESSAGES = 64
_PULL_MAX_BYTES = 4 * 1024 * 1024


def _context_item_wire(item) -> dict:
    """A scripted context item (a :class:`ContextItem` or a mapping of its
    fields) as the host's wire shape."""
    if isinstance(item, ContextItem):
        item = {"path": item.path, "renderer_id": item.renderer_id, "label": item.label,
                "text": item.text, "anchor": item.anchor}
    if not isinstance(item, dict):
        raise ValueError("a context item is a ContextItem or a mapping of its fields")
    anchor = item.get("anchor")
    if anchor is not None and not isinstance(anchor, dict):
        raise ValueError("a context item's anchor is a mapping or None")
    out = {key: str(item.get(key) or "") for key in ("path", "renderer_id", "label", "text")}
    out["anchor"] = dict(anchor) if anchor is not None else None
    return out


def _client_caps_to_dict(caps: Optional[dict]) -> dict:
    """Fill a partial capability map out to the full host ``ack_client_caps``
    shape. A test naming only the caps it cares about (``{"client":
    "electron", "native_guests": True}``) still yields every key, so the
    generator under test can read one without a guard."""
    out = dict(ipc.DEV_CLIENT_CAPS)
    for key, value in (caps or {}).items():
        out[key] = str(value) if key == "client" else bool(value)
    return out


# ---------------------------------------------------------------------------
# Credential updates obey the host's account contract.

#: What a run may commit for an account that is not ``custom`` beside the
#: credentials the account stores: its token bookkeeping.
_AUTH_STATE_FIELDS = ("expires_at", "token_type")


def _commit_refusal(snapshot: dict, secrets: dict) -> Optional[str]:
    """Why the host refuses to commit ``secrets`` for the account whose run
    snapshot is ``snapshot``, or ``None``. A run replaces only credentials
    the account already stores (the snapshot's ``secret_fields`` and
    ``secrets``) and, unless the account is ``custom``, its token
    bookkeeping, each with a nonempty string: it can neither add a field
    nor remove one."""
    removed = sorted(str(f) for f, v in secrets.items() if v is None)
    if removed:
        return f"A run cannot remove saved credentials: {', '.join(removed)}"
    malformed = sorted(str(f) for f, v in secrets.items() if not isinstance(v, str) or not v)
    if malformed:
        return f"A run commits each credential as a nonempty string, not: {', '.join(malformed)}"
    stored = set(snapshot.get("secret_fields") or ()) | set(snapshot.get("secrets") or {})
    if snapshot.get("kind") != "custom":
        stored.update(_AUTH_STATE_FIELDS)
    added = sorted(str(f) for f in secrets if f not in stored)
    if added:
        return ("A run can replace only credentials its account already stores, "
                f"not: {', '.join(added)}")
    return None


class RecordingBridge(HostBridge):
    """A HostBridge that captures cell/log/status/subprocess emissions in memory."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        workspace: str,
        *,
        prompt: str = "",
        selected_file: Optional[str] = None,
        accounts: Optional[dict] = None,
        messages: Optional[list] = None,
        autocancel_on_serve: bool = True,
        permissions: Optional[dict] = None,
        client_caps: Optional[dict] = None,
        runner: Optional[dict] = None,
        run_id: Optional[str] = None,
        label: Optional[str] = None,
        channel: str = "",
        cell_id: str = "",
        foreign_runs: Optional[dict] = None,
        ui_files: Optional[list] = None,
        packages: Optional[dict] = None,
        approvals=None,
        annotations: Optional[list] = None,
    ) -> "RecordingBridge":
        """Build a bridge rooted at ``workspace`` without running
        ``HostBridge.__init__`` (no socket, no signal handler). Mirrors the
        attribute set ``__init__`` would establish, plus the capture state.

        ``permissions`` is the environment-layer grant ``gen.grant()`` reads.
        ``client_caps`` is what ``gen.client_caps()`` reports (default: the
        dev-mode all-off answer); ``packages`` is the runner's package pins
        ``gen.packages()`` returns (default ``{}`` = nothing pinned, so a
        harness image floats to its latest release); ``approvals`` scripts
        the human answering ``gen.ask`` cards (see :meth:`answer_card`);
        ``selected_file`` is what ``gen.selected_file`` returns (the
        cell-relative path of the user's selected file; default none).

        ``annotations`` scripts the user's messages in the run's annotation
        context: a ``str`` is a queued top-level message, a dict takes the
        keywords of :meth:`add_annotation` (``text``, ``mode``, ``reply_to``,
        ``context``, ``refs``). A non-empty ``prompt`` is seeded first, as
        the host seeds it: a read ``kind="prompt"`` user message.

        Identity (what ``gen.run_info()`` returns): ``runner`` is the saved
        runner the run was started as, ``{"runner": <id>, "name":
        <display name>}`` (default: none, as for a dev run); ``run_id``
        defaults to a fresh uuid; ``label`` to ``<runner id>-<run_id[:8]>``,
        or ``dev-<run_id[:8]>`` without a runner; the run folder is
        ``_runs/<label>``; ``channel`` and ``cell_id`` default to ``""`` (a
        dev run's). ``foreign_runs`` (``{label: [path, ...]}`` or
        ``{label: {path: content}}``, paths relative to that run's folder)
        seeds sibling runs of the same cell: their files are in the
        workspace, read-only to this run. ``ui_files`` is the manifest's ``ui_files`` list the
        host would pass (e.g. ``[".chat", ".json"]``): ``gen.update_ui_file`` then
        refuses every other suffix, as a hosted run does. ``None`` (the
        default) accepts every suffix."""
        self = cls.__new__(cls)

        # --- attributes HostBridge.__init__ would set ---
        self.workspace = str(workspace)
        self.run_dir = os.path.join(self.workspace, ".collrun")
        self.permissions = dict(permissions or {})
        self.packages = dict(packages or {})
        runner = dict(runner or {})
        self.run_id = run_id or uuid.uuid4().hex
        self.channel = channel
        self.cell_id = cell_id
        self.runner_id = str(runner.get("runner") or "")
        self.runner_name = str(runner.get("name") or "")
        self.label = label or (f"{self.runner_id}-{self.run_id[:8]}" if self.runner_id
                               else f"dev-{self.run_id[:8]}")
        self.folder = f"{RUNS_PREFIX}/{self.label}"
        self.ui_suffixes = None if ui_files is None else tuple(ui_files)
        self._initialize_workspace()
        self._initialize_persistence()
        self.prompt = prompt
        self.selected_file = selected_file or None

        self.log = logging.getLogger("collimate.testing")

        # Dev-mode IPC: no socket. Any IPC method we DON'T override falls
        # back to HostBridge's dev-mode no-op (emit) / empty-ack
        # (emit_and_wait) path, so nothing hangs or crashes.
        self._initialize_ipc()

        # Cancellation self-pipe — read-only (never written) so ``cancelled``
        # stays False until we deliberately signal a stop.
        r, w = os.pipe()
        os.set_blocking(r, True)
        os.set_blocking(w, False)
        self._stop_pipe_r = r
        self._stop_pipe_w = w

        try:
            # --- capture state ---
            self.updates: list[dict] = []
            # Log artifacts flushed by update_cell (path -> content) — what a
            # host would have routed into run_artifacts(run_id, 'log', ...).
            self.log_files: dict = {}
            self._rec_endpoints: list[Endpoint] = []
            self._rec_tier_b: list[dict] = []
            self._rec_logs: list[str] = []
            self._rec_accounts: dict = dict(accounts or {})
            # Scripted inbound chat commands for a host-free ``gen.serve_chat``
            # test: each ``chat_poll`` pops one. Plain strings are normalised to
            # ``{"kind": "user_msg", "content": ...}``; a dict passes through so a
            # test can script interrupts. Drained → stop the run.
            self._rec_messages: deque = deque(messages or [])
            # Stream events the run pushed to its inbox (turn_start /
            # text_delta / tool / turn_end / error / approval cards), captured
            # for assertion: ``{"turn_id", "event"}``.
            self.stream_events: list[dict] = []
            # Every (phase, message) the run set, in order.
            self.phases: list = []
            # The scripted human behind gen.ask: None = nobody can answer.
            self.stub_approvals(approvals)
            # Terminal sessions a test (or stop_terminal) marked as ended — what
            # ``terminal_status`` reports back.
            self._terminals_ended: set = set()
            self._chat_session_counter = 0
            # The run's inbox surface (``serve_chat`` registers it; one per run).
            self._inbox_surface: Optional[str] = None
            # Files this run asked the clients to show (gen.reveal_file), in
            # order.
            self.revealed: list = []
            # The run's annotation context: wire Message dicts in insertion
            # order, its revision and interrupt counter, the remembered acks of
            # pulls and posts (newest 64), and what the run posted
            # (``{"id", "text", "reply_to"}`` each).
            self.annotation_posts: list = []
            self.annotations_listening = False
            self._annotation_store_lock = threading.Lock()
            self._annotation_messages: list = []
            self._annotation_rev = 0
            self._annotation_interrupt_seq = 0
            self._annotation_acks: dict = {}
            # Harness activity (a pull or post, a stream event, a commit) and
            # the activity count at the last idle chat_poll: a listening run
            # stops on the second idle poll with none in between.
            self._activity = 0
            self._idle_mark: Optional[int] = None
            self._autocancel_on_serve = autocancel_on_serve
            self._update_counter = 0
            self._term_counter = 0
            # Sibling runs of this cell: label -> the files of its folder.
            self.foreign_runs: dict = {}
            for other, files in (foreign_runs or {}).items():
                self.add_foreign_run(other, files)
            self._stub_client_caps = _client_caps_to_dict(client_caps)
            # Host-placed environment ops (env_place/env_exec/…) in call order.
            self._rec_env_ops: list = []
            self._stub_handlers = []
            self._stub_handler_skills = {}
            self._stub_verifiers = {}
            self._stub_env_exec = None
            self._stub_env_place = None
            self._cell_files = self._snapshot(self.fs_list())
            if prompt.strip():
                self._store_annotation(author="user", kind="prompt", body=prompt,
                                       born_read=True)
            for entry in annotations or ():
                if isinstance(entry, str):
                    self.add_annotation(entry)
                elif isinstance(entry, dict):
                    self.add_annotation(**entry)
                else:
                    raise TypeError("an annotations entry is a str or a dict of "
                                    "add_annotation keywords")
        except BaseException:
            # Release the self-pipe of the half-built bridge (nobody gets
            # it to close), e.g. when add_foreign_run rejects a label.
            self.close()
            raise

        return self

    # ------------------------------------------------------------------
    # Sibling runs (foreign run folders in the same cell)
    # ------------------------------------------------------------------

    def add_foreign_run(self, label: str, files) -> None:
        """Seed a sibling run of this cell, labelled ``label``: its folder's
        files (``[path, ...]`` or ``{path: content}``, paths relative to
        ``_runs/<label>/``) are written into the workspace as the host's
        hydration would, and are read-only to this run."""
        if not label or label == self.label:
            raise ValueError(f"foreign run label {label!r} must name another run")
        items = files.items() if isinstance(files, dict) else ((p, "") for p in files or ())
        folder = f"{RUNS_PREFIX}/{label}"
        paths = self.foreign_runs.setdefault(label, [])
        for rel, content in items:
            path = f"{folder}/{str(rel).lstrip('/')}"
            full = os.path.join(self.workspace, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(full, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as fh:
                fh.write(content)
            paths.append(path)

    # ------------------------------------------------------------------
    # Composition stubs (set by tests)
    # ------------------------------------------------------------------


    def stub_handlers(self, handlers, *, skills=None) -> None:
        """Supply public handler metadata and accepted namespaced skills."""
        import copy
        self._stub_handlers = copy.deepcopy(handlers)
        self._stub_handler_skills = copy.deepcopy(skills or {})

    def stub_verifier(self, ext: str, fn) -> None:
        """Supply a byte-content verifier for a suffix (or explicit path)."""
        self._stub_verifiers[ext.casefold()] = fn

    def handlers(self) -> list:
        import copy
        return copy.deepcopy(self._stub_handlers)

    def handler_skills(self, ext_or_id: str) -> dict[str, str]:
        from collimate.handlers import handler_for
        record = next((h for h in self._stub_handlers if h["id"] == ext_or_id), None)
        record = record or handler_for(self._stub_handlers, ext_or_id)
        return dict(self._stub_handler_skills.get(record["id"], {})) if record else {}

    def validate(self, path: str, verifier: str | None = None) -> dict:
        content = self.fs_read_bytes(path)
        if verifier is not None:
            self.fs_read_bytes(verifier)
            fn = self._stub_verifiers.get(verifier.casefold())
            if fn is None:
                raise RuntimeError(f"no stub for explicit verifier: {verifier}")
        else:
            matches = [(ext, fn) for ext, fn in self._stub_verifiers.items() if path.casefold().endswith(ext)]
            fn = max(matches, key=lambda pair: len(pair[0]))[1] if matches else None
        return fn(content) if fn else {"valid": True}

    def stub_client_caps(self, caps) -> None:
        """Make :func:`gen.client_caps` report these. Partial maps are filled
        out to the full shape; ``None`` restores the dev-mode all-off answer."""
        self._stub_client_caps = _client_caps_to_dict(caps)


    def stub_approvals(self, approvals) -> None:
        """Script the human answering ``gen.ask`` cards: ``None`` (nobody can
        answer), a list of answers in order, or ``fn(card) -> answer`` — see
        :meth:`answer_card`."""
        self._approvals = (approvals if approvals is None or callable(approvals)
                           else deque(approvals))


    def stub_env_exec(self, fn) -> None:
        """Set host-placed environment exec behaviour:
        ``fn(env_id, command, kwargs) -> (rc, output) | dict``. Together with
        ``env_place`` (which returns a synthetic placement unless
        :meth:`stub_env_place` overrides it) this lets
        ``_env_shared.DockerEnvironment`` be driven host-free."""
        self._stub_env_exec = fn

    def stub_env_place(self, fn) -> None:
        """Set placement behaviour: ``fn(spec) -> dict`` (the ack shape).
        Return ``{"ok": False, "error": …}`` to simulate the host failing to
        place the environment — the mediation-era stand-in for the old
        'docker daemon unreachable' scenario."""
        self._stub_env_place = fn

    # ------------------------------------------------------------------
    # Host-placed environment overrides (in-process)
    # ------------------------------------------------------------------

    def env_place(self, spec: dict) -> dict:
        self._rec_env_ops.append({"op": "env_place", "spec": dict(spec or {})})
        fn = self._stub_env_place
        if fn is not None:
            return fn(spec)
        return {
            "ok": True,
            "env_id": f"env-{len(self._rec_env_ops)}",
            "container_id": "recorded-container",
            "error": None,
        }

    def env_exec(self, env_id, command, *, cwd=None, env=None, timeout=None,
                 user=None, detach=False) -> dict:
        self._rec_env_ops.append({
            "op": "env_exec", "env_id": env_id, "command": command,
            "cwd": cwd, "env": env, "timeout": timeout, "user": user,
            "detach": detach,
        })
        fn = self._stub_env_exec
        if fn is not None:
            res = fn(env_id, command, {"cwd": cwd, "env": env, "timeout": timeout,
                                       "user": user, "detach": detach})
            if isinstance(res, tuple) and len(res) == 2:
                return {"ok": True, "rc": int(res[0]), "output": str(res[1]), "error": None}
            if isinstance(res, dict):
                return res
        return {"ok": True, "rc": 0, "output": "", "error": None}

    def env_expose_port(self, env_id, port: int) -> dict:
        self._rec_env_ops.append({"op": "env_expose_port", "env_id": env_id, "port": int(port)})
        return {"ok": True, "host_port": 40000 + int(port) % 1000, "error": None}

    def env_close(self, env_id) -> dict:
        self._rec_env_ops.append({"op": "env_close", "env_id": env_id})
        return {"ok": True, "error": None}

    @property
    def env_ops(self) -> list:
        """Every host-placed-environment op this bridge saw, in order."""
        return list(self._rec_env_ops)

    # ------------------------------------------------------------------
    # Composition method overrides (in-process)
    # ------------------------------------------------------------------


    def client_caps(self) -> dict:
        return dict(self._stub_client_caps)


    def account(self, role: str) -> dict:
        from copy import deepcopy
        if role not in self._rec_accounts:
            raise LookupError(f"No account bound for role {role!r}")
        return deepcopy(self._rec_accounts[role])

    def commit_account(self, role: str, expected_revision: int, secrets: dict) -> dict:
        """The host's compare-and-swap commit: the values are merged into
        the fixture's ``secrets``. A field the account does not store, a
        ``None`` (a removal) or an empty or non-string value raises
        :class:`RuntimeError` and changes nothing (:func:`_commit_refusal`)."""
        current = self.account(role)
        if current.get("revision", 0) != expected_revision:
            raise RuntimeError("Account revision conflict")
        secrets = dict(secrets)
        refusal = _commit_refusal(current, secrets)
        if refusal:
            raise RuntimeError(refusal)
        current["secrets"] = {**(current.get("secrets") or {}), **secrets}
        current["revision"] = expected_revision + 1
        self._rec_accounts[role] = current
        return {"committed": True, "revision": current["revision"]}


    def close(self) -> None:
        """Release the cancellation self-pipe. Idempotent."""
        for attr in ("_stop_pipe_r", "_stop_pipe_w"):
            fd = getattr(self, attr, -1)
            if isinstance(fd, int) and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, -1)

    def _signal_stop(self) -> None:
        """Flip ``cancelled`` to True (write one byte to the self-pipe)."""
        try:
            os.write(self._stop_pipe_w, b"x")
        except (BlockingIOError, OSError):
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _snapshot(self, paths) -> dict:
        out: dict = {}
        for p in paths:
            try:
                if self.fs_exists(p):
                    out[p] = self.fs_read_any(p)
            except OSError:
                continue
        return out

    # ------------------------------------------------------------------
    # Cell operations — capture instead of emit
    # ------------------------------------------------------------------

    def read_cell_files(self, *, paths: list[str] | None = None,
                        revisions: dict[str, int] | None = None) -> dict:
        selected = self._cell_files if paths is None else {
            p: self._cell_files[p] for p in paths if p in self._cell_files}
        return dict(selected)

    def read_cell_descriptors(self, *, paths: list[str] | None = None) -> dict:
        return {p: {"body_hash": digest, "file_revision": self._update_counter,
                    "encoding": "utf-8" if isinstance(self._cell_files[p], str) else "binary"}
                for p, digest in self.read_cell_hashes(paths=paths).items()}

    def read_cell_hashes(self, *, paths: list[str] | None = None) -> dict[str, str]:
        import hashlib

        return {p: hashlib.sha256(c.encode("utf-8") if isinstance(c, str) else c).hexdigest()
                for p, c in self.read_cell_files(paths=paths).items()}

    def _commit_update(
        self,
        *,
        name: Optional[str] = None,
        writes,
        deleted_files: Optional[list] = None,
    ) -> str:
        ws = set(writes)
        # The host's partition first: a write or delete it would refuse (a
        # path in another run's folder, the run card, an own-folder file of
        # an undeclared type) is dropped and named, the rest is committed.
        refused = [
            entry for entry in (
                self._host_refusal(p, write=p in ws
                                   and p not in self.log_paths)
                for p in sorted(ws | set(deleted_files or ())))
            if entry
        ]
        refused_paths = {r["path"] for r in refused}
        # Mirror HostBridge.update_cell's routing: log-declared paths go to
        # the run's log_files record, everything else to the cell snapshot.
        committed = ws - refused_paths
        cell_paths = sorted(p for p in committed if p not in self.log_paths)
        log_flush = sorted(p for p in committed if p in self.log_paths)
        files = self._snapshot(cell_paths)
        self.log_files.update(self._snapshot(log_flush))
        self._cell_files.update(files)
        for path in set(deleted_files or ()) - refused_paths:
            self._cell_files.pop(path, None)
        self._update_counter += 1
        self._activity += 1
        self.updates.append({"name": name, "files": files})
        if refused:
            raise PartialRefusal(ipc.refused_message(refused))
        return f"rec-update-{self._update_counter}"

    def _host_refusal(self, path: str, *, write: bool) -> Optional[dict]:
        """The ``refused`` entry the host's emit partition would name for
        ``path``, or ``None`` when it would accept it (``write``: a cell
        write, not a delete or a log artifact)."""
        if path == run_card_path(self.label):
            return {"path": path, "reason": "host_owned", "owner": self.label}
        if not is_run_path(path):
            return None
        if not path.startswith(self.folder + "/"):
            return {"path": path, "reason": "locked", "owner": run_label(path)}
        if write and not self.ui_type_declared(path):
            return {"path": path, "reason": "ui_type", "owner": self.label}
        return None


    # ------------------------------------------------------------------
    # Status / control — capture
    # ------------------------------------------------------------------

    def push_log(self, line: str) -> None:
        self._rec_logs.append(line)

    def set_phase(self, phase: str, message: str = "") -> None:
        self.phases.append((phase, message))

    def progress(self, current: int, total: int) -> None:
        # No IPC; nothing to record on the result. No-op.
        return None

    # ------------------------------------------------------------------
    # Live chat — scripted inbound queue; stop when drained
    # ------------------------------------------------------------------

    def chat_poll(self) -> Optional[dict]:
        """Pop the next scripted inbound chat command, or stop the run when
        the queue is drained so ``serve_chat`` exits cleanly. Plain strings
        are normalised to a ``user_msg`` command; dicts pass through (so a
        test can script ``interrupt``)."""
        if self._rec_messages:
            cmd = self._rec_messages.popleft()
            if isinstance(cmd, dict):
                return cmd
            return {"kind": "user_msg", "content": str(cmd)}
        # Scripted queue drained — but do NOT stop while a turn is still in
        # flight, the session's own turn queue (an annotation batch, a
        # validator correction) has work, or a scripted annotation waits to
        # be pulled: a long agentic turn seeded from `initial_message` must
        # run to completion, not be cut off mid-write. Stop only once the
        # session is genuinely idle. A run that listens to its annotations
        # stops on the second idle poll with no harness activity between, so
        # a batch just pulled has the time to become a turn. The run-level
        # timeout is the backstop against a livelock.
        try:
            from collimate import native
            busy = native.chat_busy()
        except Exception:  # noqa: BLE001 — bridge-free / import race
            busy = False
        if busy or self._unread_annotations():
            self._idle_mark = None
            return None
        if self.annotations_listening and self._idle_mark != self._activity:
            self._idle_mark = self._activity
            return None
        self._signal_stop()
        return None

    def stream_event(self, turn_id: str, event: dict) -> None:
        """Record a streamed event instead of pushing it to the run's inbox.
        Asserted via ``res.tier_b_calls`` / the bridge's
        :attr:`stream_events`."""
        rec = {"turn_id": turn_id, "event": dict(event or {})}
        self._activity += 1
        self.stream_events.append(rec)
        self._rec_tier_b.append({"op": "stream_event", **rec})
        if rec["event"].get("type") == "permission_request":
            self.answer_card(rec["event"])

    # ------------------------------------------------------------------
    # Approval cards — the scripted human
    # ------------------------------------------------------------------

    def answers_cards(self) -> bool:
        """Someone can answer ``gen.ask`` cards exactly when the test
        scripted ``approvals``. As with a real host, ``gen.ask`` still shows
        a card only while ``gen.serve_chat`` serves the run's chat session."""
        return self._approvals is not None

    def answer_card(self, card: dict) -> None:
        """Answer an approval card the way the renderer would (through the
        chat session's command inbox, as serve_chat delivers it), from the
        ``approvals`` script: a list gives one answer per card in order, a
        callable ``fn(card)`` answers each. An answer is the chosen option id
        (``str``), ``None`` for a dismissed card, or :data:`UNANSWERED` to
        leave it waiting; a list that has run out leaves further cards
        unanswered."""
        script = self._approvals
        if script is None:
            return
        if callable(script):
            answer = script(card)
        else:
            answer = script.popleft() if script else UNANSWERED
        if answer is UNANSWERED:
            return
        from collimate import native
        reply = {"id": card.get("id", "")}
        if answer is None:
            reply["cancelled"] = True
        else:
            reply["option_id"] = str(answer)
        native.deliver_permission_answer(reply)

    def surface_register(self, path: str, *, label: str, transport_only: bool,
                         broadcast: bool) -> dict:
        """Record the run's inbox registration, checked as the host bridge
        checks it; one inbox per run, as on the host."""
        declaration = _inbox_declaration(path, folder=self.folder, label=label,
                                         transport_only=transport_only,
                                         broadcast=broadcast)
        if self._inbox_surface is not None:
            raise RuntimeError(f"surface_register({path!r}) failed: inbox_exists")
        self._inbox_surface = declaration["path"]
        self._chat_session_counter += 1
        sid = f"rec-surface-{self._chat_session_counter}"
        self._rec_tier_b.append({"op": "surface_register", "chat_id": sid,
                                 **declaration, "transport_only": transport_only,
                                 "broadcast": broadcast})
        return {"chat_id": sid, "ws_path": f"/rec/ws/{sid}", "token": "rec-token"}

    def reveal_file(self, path: str) -> None:
        """Record a reveal. ``committed`` is whether the host would have
        shown it: the path was flushed (not staged) and exists."""
        committed = (path not in self.tracked_writes
                     and path not in self.tracked_deletions
                     and self.fs_exists(path))
        rec = {"op": "reveal_file", "path": path, "committed": committed}
        self.revealed.append(path)
        self._rec_tier_b.append(rec)

    # ------------------------------------------------------------------
    # The annotation context — in memory, ordered as the host orders it
    # ------------------------------------------------------------------

    def annotations_available(self) -> bool:
        return True

    def add_annotation(self, text: str, *, mode: str = "queued",
                       reply_to: Optional[str] = None, context=(), refs=()) -> str:
        """Add one user message to the run's annotation context (mid-run
        too) and return its id: a top-level message, or a reply to the
        top-level message ``reply_to``. ``mode="interrupt"`` places it ahead
        of the unread queue and advances ``interrupt_seq``, as a user's
        interrupt-mode message does on the host. ``context`` holds
        :class:`~collimate._results.ContextItem` values or mappings of their
        fields; ``refs`` the cell paths the text links."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("add_annotation: text must be non-empty")
        if mode not in ipc.ANNOTATION_MODES:
            raise ValueError(f"add_annotation: mode must be one of {sorted(ipc.ANNOTATION_MODES)}")
        items = [_context_item_wire(item) for item in context or ()]
        if any(not isinstance(ref, str) for ref in refs or ()):
            raise ValueError("add_annotation: refs must be cell paths")
        with self._annotation_store_lock:
            if reply_to is not None and self._annotation_root(reply_to) is None:
                raise ValueError(f"add_annotation: {reply_to!r} is not a top-level message")
            message = self._store_annotation(
                author="user", kind="message", body=text, mode=mode,
                parent_id=reply_to, context_items=items, refs=list(refs or ()))
            if mode == "interrupt":
                self._annotation_interrupt_seq += 1
            return message["id"]

    def _store_annotation(self, *, author: str, kind: str, body: str,
                          mode: str = "queued", parent_id: Optional[str] = None,
                          context_items=(), refs=(), born_read: bool = False,
                          message_id: Optional[str] = None,
                          author_label: Optional[str] = None) -> dict:
        """Insert one wire Message and bump the revision (store lock held,
        or during construction)."""
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        if parent_id is not None:
            position = 0.0
        else:
            position = self._annotation_place("head" if mode == "interrupt" else "tail")
        message = {
            "id": message_id or ipc.new_annotation_id(), "context_id": self.run_id,
            "parent_id": parent_id, "author": author, "author_label": author_label,
            "kind": kind, "body": body, "context_items": [dict(item) for item in context_items],
            "refs": list(refs), "mode": mode, "position": position, "held": False,
            "revision": 1, "created_at": now, "edited_at": None,
            "read_at": now if born_read else None,
        }
        self._annotation_messages.append(message)
        self._annotation_rev += 1
        return message

    def _annotation_root(self, message_id: str) -> Optional[dict]:
        return next((m for m in self._annotation_messages
                     if m["id"] == message_id and m["parent_id"] is None), None)

    def _annotation_place(self, where: str) -> float:
        """The host's placement: ``tail`` after every top-level message,
        ``head`` just before the first unread one (``tail`` when none is)."""
        tops = sorted(m["position"] for m in self._annotation_messages if m["parent_id"] is None)
        unread = [m["position"] for m in self._annotation_messages
                  if m["parent_id"] is None and m["read_at"] is None]
        if where == "head" and unread:
            hi = min(unread)
            below = [p for p in tops if p < hi]
            if not below:
                return hi - 1
            position = (max(below) + hi) / 2
            if max(below) < position < hi:
                return position
            # Crowded: number the top-level messages 1..N in order, then retry.
            ordered = sorted((m for m in self._annotation_messages if m["parent_id"] is None),
                             key=lambda m: m["position"])
            for number, message in enumerate(ordered, 1):
                message["position"] = float(number)
            return self._annotation_place(where)
        return (tops[-1] + 1) if tops else 1.0

    def _annotation_order(self) -> list:
        """The context's messages: top-level by position, each followed by its
        replies in insertion order."""
        index = {m["id"]: i for i, m in enumerate(self._annotation_messages)}
        out = []
        for root in sorted((m for m in self._annotation_messages if m["parent_id"] is None),
                           key=lambda m: (m["position"], index[m["id"]])):
            out.append(root)
            out.extend(m for m in self._annotation_messages if m["parent_id"] == root["id"])
        return out

    def _unread_annotations(self) -> bool:
        with self._annotation_store_lock:
            return any(m["read_at"] is None for m in self._annotation_messages)

    def _remember_annotation_ack(self, operation_id: str, ack: dict) -> dict:
        self._annotation_acks[operation_id] = ack
        while len(self._annotation_acks) > 64:
            self._annotation_acks.pop(next(iter(self._annotation_acks)))
        return copy.deepcopy(ack)

    def _annotations_ack(self, op: str, **fields) -> dict:
        """Answer an annotation op as the host does."""
        with self._annotation_store_lock:
            if op == "annotations_poll":
                if not self.annotations_listening:
                    self.annotations_listening = True
                    self._annotation_rev += 1
                return {"op": "ack_annotations_poll", "ok": True, **self._annotation_notice(),
                        "listening": True}
            if op == "annotations_list":
                return {"op": "ack_annotations_list", "ok": True,
                        "messages": copy.deepcopy(self._annotation_order())}
            operation_id = fields.get("operation_id")
            if operation_id in self._annotation_acks:
                return copy.deepcopy(self._annotation_acks[operation_id])
            if op == "annotations_pull":
                return self._pull_annotations(operation_id)
            if op == "annotations_post":
                return self._post_annotation(operation_id, fields)
        raise ValueError(f"unknown annotation op {op!r}")

    def _annotation_notice(self) -> dict:
        """The context's ``{rev, unread, interrupt_seq}`` (store lock held)."""
        return {"rev": self._annotation_rev,
                "unread": sum(m["read_at"] is None for m in self._annotation_messages),
                "interrupt_seq": self._annotation_interrupt_seq}

    def _pull_annotations(self, operation_id: str) -> dict:
        messages = self._annotation_messages
        index = {m["id"]: i for i, m in enumerate(messages)}
        by_id = {m["id"]: m for m in messages}
        units = []
        for message in messages:
            if message["read_at"] is not None:
                continue
            if message["parent_id"] is None:
                units.append((message, message, [message] + [
                    r for r in messages if r["parent_id"] == message["id"] and r["read_at"] is None]))
            elif by_id[message["parent_id"]]["read_at"] is not None:
                units.append((message, by_id[message["parent_id"]], [message]))
        units.sort(key=lambda unit: (unit[0]["mode"] != "interrupt", unit[1]["position"],
                                     unit[0]["created_at"], index[unit[0]["id"]]))
        batch: list = []
        size = 0
        for _lead, _root, members in units:
            weight = sum(len(m["body"].encode("utf-8")) for m in members)
            if batch and (len(batch) + len(members) > _PULL_MAX_MESSAGES
                          or size + weight > _PULL_MAX_BYTES):
                break
            batch.extend(members)
            size += weight
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        for message in batch:
            message["read_at"] = now
        if batch:
            self._annotation_rev += 1
            self._activity += 1
            self._rec_tier_b.append({"op": "annotations_pull", "ids": [m["id"] for m in batch]})
        return self._remember_annotation_ack(operation_id, {
            "op": "ack_annotations_pull", "ok": True, "operation_id": operation_id,
            "messages": copy.deepcopy(batch), **self._annotation_notice()})

    def _post_annotation(self, operation_id: str, fields: dict) -> dict:
        import re
        body, reply_to = fields.get("body"), fields.get("reply_to")
        message_id = fields.get("message_id")

        def refused(error: str) -> dict:
            return {"op": "ack_annotations_post", "ok": False, "error": error}

        if not isinstance(body, str) or not body.strip():
            return refused("empty_text")
        if len(body.encode("utf-8")) > ipc.ANNOTATION_BODY_MAX_BYTES:
            return refused("too_large")
        if not isinstance(message_id, str) or not re.fullmatch(ipc.ANNOTATION_ID_PATTERN, message_id):
            return refused("invalid")
        if any(m["id"] == message_id for m in self._annotation_messages):
            return refused("duplicate_id")
        if reply_to is not None:
            target = next((m for m in self._annotation_messages if m["id"] == reply_to), None)
            if target is None:
                return refused("unknown_message")
            if target["parent_id"] is not None:
                return refused("reply_depth")
        self._store_annotation(author="run", kind="message", body=body, parent_id=reply_to,
                               born_read=True, message_id=message_id,
                               author_label=self.label)
        self._activity += 1
        self.annotation_posts.append({"id": message_id, "text": body, "reply_to": reply_to})
        self._rec_tier_b.append({"op": "annotations_post", "id": message_id, "text": body,
                                 "reply_to": reply_to})
        return self._remember_annotation_ack(operation_id, {
            "op": "ack_annotations_post", "ok": True, "operation_id": operation_id,
            "id": message_id})

    def annotation_context(self) -> list[Annotation]:
        """The run's whole annotation context, in context order."""
        with self._annotation_store_lock:
            return [annotation_from_wire(m) for m in self._annotation_order()]

    def poll_renderer_action_event(self, *, source: str = "",
                                   action_id: str = "") -> Optional[dict]:
        self._rec_tier_b.append({
            "op": "poll_renderer_action",
            **({"source": source} if source else {}),
            **({"action_id": action_id} if action_id else {}),
        })
        return None


    # ------------------------------------------------------------------
    # Daemons / subprocess — record structurally, don't execute
    # ------------------------------------------------------------------

    def serve_port(
        self,
        port: int,
        *,
        name: str = "preview",
        path: str = "",
    ) -> None:
        self._rec_tier_b.append({"op": "serve_port", "port": port, "name": name, "path": path})
        self._rec_endpoints.append(Endpoint(kind="http", name=name, port=port, path=path))
        if self._autocancel_on_serve:
            self._signal_stop()  # let a daemon's `while not cancelled` loop exit

    def start_terminal(
        self,
        *,
        container_id: str,
        command: list,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        keep_alive: bool = False,
    ) -> dict:
        self._term_counter += 1
        sid = f"rec-term-{self._term_counter}"
        self._rec_tier_b.append({
            "op": "start_terminal", "container_id": container_id,
            "command": list(command), "cwd": cwd or "",
            **({"keep_alive": True} if keep_alive else {}),
        })
        self._rec_endpoints.append(Endpoint(kind="terminal", name="terminal", terminal_id=sid))
        if self._autocancel_on_serve:
            self._signal_stop()
        return {"terminal_id": sid, "ws_path": f"/rec/ws/{sid}", "token": "rec-token"}

    def start_host_terminal(
        self,
        *,
        command: list,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        keep_alive: bool = False,
    ) -> dict:
        self._term_counter += 1
        sid = f"rec-term-{self._term_counter}"
        self._rec_tier_b.append({
            "op": "start_host_terminal", "command": list(command), "cwd": cwd or "",
            "env_names": sorted(env or {}),
            **({"keep_alive": True} if keep_alive else {}),
        })
        self._rec_endpoints.append(Endpoint(kind="terminal", name="terminal", terminal_id=sid))
        if self._autocancel_on_serve:
            self._signal_stop()
        return {"terminal_id": sid, "ws_path": f"/rec/ws/{sid}", "token": "rec-token"}

    def stop_terminal(self, terminal_id: str) -> None:
        self._rec_tier_b.append({"op": "stop_terminal", "terminal_id": terminal_id})
        self._terminals_ended.add(terminal_id)

    def terminal_status(self, terminal_id: str) -> dict:
        """Recorded stub: a terminal is live until ``stop_terminal`` (or a
        test marks it ended via ``_terminals_ended``)."""
        ended = terminal_id in self._terminals_ended
        return {"exists": not ended, "ended": ended, "exit_code": 0 if ended else None}
