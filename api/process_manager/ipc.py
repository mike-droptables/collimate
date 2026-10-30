"""IPC protocol — TCP, JSON-per-line (loopback for native runs, 0.0.0.0 for
docker runs; every connection opens with the run's per-run token frame, see
backends.make_authed_ipc_handler).

All ops the generator can invoke on the host run through ``_handle_ipc``.
Every request carries a nonempty string ``req_id`` echoed unchanged by its
ack or NAK. Host dispatch remains serial. Known requests receive replies::

    {"op": "nak", "for_op": <str>, "reason": <str>, "req_id": <str>}

One-way messages never receive replies. Invalid frames are logged and the
connection closes; no unmatched reply can poison a later request.
"""
# pyright: reportAttributeAccessIssue=false
# (mixin: attributes like log_buffer/workspace_dir live on the composed
# ProcessManager in manager.py, not on the mixin class itself)

import asyncio
import json
import logging
import os


from collimate._host.ipc import FINISH_OUTCOMES, IPC_MAX_LINE_BYTES, OPS

from database import get_db_connection
from run_ends import mark_run_ended
from versions import version_file_hashes


_logger = logging.getLogger("process_manager.ipc")


class ContainerNotOwned(RuntimeError):
    """A run named a container that is not one of its own environments."""


class HostTerminalRefused(RuntimeError):
    """A run asked for a terminal on this machine its grant or its harness
    placement does not allow."""


# Both derived from the SDK's OPS, the one list of ops. Only requests have a
# caller waiting for a reply: a failed notification is logged without
# leaving an unsolicited NAK on the stream. OP_CAPABILITY is the grant
# capability a gated op needs before its handler runs.
REQUEST_OPS = frozenset(op for op, spec in OPS.items() if spec.kind == "request")
OP_CAPABILITY = {op: spec.capability for op, spec in OPS.items() if spec.capability is not None}

# The run's annotation context: pull receipts kept per manager, and the
# opaque operation ids a pull or post carries.
ANNOTATION_PULL_RECEIPTS = 64
_OPERATION_ID_MAX = 256


class IPCMixin:


    async def _send_nak(self, for_op: str, reason: str, *, req_id: str, **extra) -> None:
        """Write a NAK frame to the IPC channel.

        Failing to write (writer torn down mid-handshake) is logged but
        not raised — there's nothing useful to do once the channel is
        gone, and the SDK will see EOF and surface its own error.
        """
        if self._ipc_writer is None:
            return
        try:
            frame = json.dumps({"op": "nak", "for_op": for_op, "reason": reason, **extra, "req_id": req_id}) + "\n"
            self._ipc_writer.write(frame.encode())
            await self._ipc_writer.drain()
        except Exception as exc:
            self.log_buffer.append({
                "type": "stderr",
                "payload": f"[IPC] failed to send NAK ({reason!r} for {for_op!r}): {exc}",
            })

    async def _handle_ipc_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle the generator's TCP connection for IPC messages."""
        self._ipc_writer = writer
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # Line exceeded the StreamReader limit (raised as
                    # ValueError, wrapping LimitOverrunError). The stream
                    # is mid-frame and can't be resynced — drop
                    # the connection rather than keep buffering.
                    self.log_buffer.append({
                        "type": "ipc_error",
                        "payload": f"oversize: line exceeds {IPC_MAX_LINE_BYTES} bytes",
                    })
                    break
                if not line:
                    break
                if len(line) > IPC_MAX_LINE_BYTES:
                    # Belt-and-suspenders behind the reader limit; close
                    # rather than keep reading from a peer that ignores
                    # the frame cap.
                    self.log_buffer.append({
                        "type": "ipc_error",
                        "payload": f"oversize: {len(line)} bytes > {IPC_MAX_LINE_BYTES}",
                    })
                    break
                if await self._handle_ipc(line.decode("utf-8", errors="replace").strip()) is False:
                    break
        except Exception as e:
            self.log_buffer.append({"type": "stderr", "payload": f"[IPC Error] {e}"})
        finally:
            # Clear the shared writer first (only if it is still this
            # connection's), so an abrupt peer reset in wait_closed() below
            # can't leave a dead writer installed.
            if self._ipc_writer is writer:
                self._ipc_writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # IPC dispatch
    #
    # Every op is dispatched through the name → handler table in the
    # ``_ipc_handlers`` property: ``_op_<name>`` for each op of the SDK's
    # OPS, whose comments group the op families. OP_CAPABILITY (above) is
    # the single permission gate, consulted between lookup and dispatch.
    # Unknown ops fall through to ``_unknown_op``: tagged ipc_error in the
    # logs, and the raw frame is persisted for debugging under the fixed,
    # non-rendered type ``ipc_unknown_op``.
    # ------------------------------------------------------------------

    async def _handle_ipc(self, line: str):
        """Process a single IPC JSON message."""
        if not line:
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            # Truncate the bad payload in the log to avoid drowning the
            # ipc_error stream when a generator emits a few MB of garbage.
            preview = line[:200] + ("…" if len(line) > 200 else "")
            self.log_buffer.append({
                "type": "ipc_error",
                "payload": f"JSON decode error: {exc.msg}; preview: {preview!r}",
            })
            return False

        if not isinstance(data, dict):
            self.log_buffer.append({"type": "ipc_error", "payload": "IPC frame must be an object"})
            return False

        op = data.get("op")
        handler = self._ipc_handlers.get(op) if isinstance(op, str) else None
        if handler is None:
            await self._unknown_op(data)
            return
        if op in REQUEST_OPS and not (
            isinstance(data.get("req_id"), str) and data["req_id"]
        ):
            self.log_buffer.append({
                "type": "ipc_error", "payload": f"{op}: nonempty string req_id required",
            })
            return False
        assert isinstance(op, str)  # A selected handler implies a string operation.
        # Permission chokepoint: the ONE host gate. Consult OP_CAPABILITY
        # between op lookup and dispatch; a denial short-circuits before the
        # handler runs (no state mutation on a denied op).
        try:
            capability = OP_CAPABILITY.get(op)
            if capability is not None and not self.permissions.allows(capability):
                self.log_buffer.append({
                    "type": "ipc_error", "payload": f"{op}: permission_denied ({capability})",
                })
                if op in REQUEST_OPS:
                    await self._send_nak(op, "permission_denied", capability=capability, req_id=data["req_id"])
                return
            await handler(data)
        except Exception as exc:
            self.log_buffer.append({"type": "ipc_error", "payload": f"{op}: handler_error: {exc}"})
            if op in REQUEST_OPS:
                await self._send_nak(op, "handler_error", error=str(exc), req_id=data["req_id"])


    @property
    def _ipc_handlers(self):
        """The op → handler table: ``_op_<name>`` for every op in the SDK's
        OPS. Built on first use and cached on the instance, so dispatch is
        O(1) per message."""
        cached = getattr(self, "_ipc_handlers_cache", None)
        if cached is None:
            cached = self._ipc_handlers_cache = {op: getattr(self, f"_op_{op}") for op in OPS}
        return cached

    async def _send_ack(self, ack_body: dict, request: dict) -> None:
        """Write an ack frame back to the IPC writer. No-op if the
        writer was torn down (e.g. process died mid-handle)."""
        if self._ipc_writer is None:
            return
        frame = {**ack_body, "req_id": request["req_id"]}
        if ack_body.get("op") == "ack_read_cell_files":
            encoded = await asyncio.to_thread(lambda: (json.dumps(frame) + "\n").encode())
        else:
            encoded = (json.dumps(frame) + "\n").encode()
        self._ipc_writer.write(encoded)
        await self._ipc_writer.drain()

    # ------------------------------------------------------------------
    # Cell-write ops
    # ------------------------------------------------------------------

    async def _op_update_cell(self, data: dict) -> None:
        """Dispatch through ``OwnCellWritesMixin._emit_partial`` — the single
        partition → conditional mutation → broadcast sequence. An accepted
        file change appends immutable history; exact replay and metadata-only
        requests reuse their receipts without another version. The ack names
        the paths the run folders refused
        (``refused: [{path, reason, owner}]``); the rest landed.

        The write runs on a worker thread tracked in ``_inflight_emits``:
        the run-end sweep waits for every one of them, so no emit lands
        after the run's folder is gone. The paths it committed are noted
        (:meth:`_note_own_commit`) so the chat flush never copies the run's
        own commit back over a newer workspace write."""
        # A cell write is real activity ⇒ advance spinning_up → running
        # (no-ops once past spinning_up, and won't clobber an explicit phase).
        self._maybe_auto_running()
        self._last_file_receipt = {}
        refused: list = []
        version_id = ""
        error = None
        task = asyncio.ensure_future(asyncio.to_thread(
            self._emit_partial,
            artifacts=data.get("artifacts") or [],
            cell_name=(data.get("metadata") or {}).get("name"),
            deleted_files=data.get("deleted_files"),
            operation_id=data.get('operation_id'),writer_id=data.get('writer_id'),edit_seq=data.get('edit_seq'),
            base_revisions=data.get('base_revisions'),resolves=data.get('resolves'),
        ))
        self._inflight_emits.add(task)
        try:
            version_id, refused = await asyncio.shield(task)
            if version_id:
                self._note_own_commit(data,refused,version_id)
        except Exception as exc:
            error = str(exc)
            self.log_buffer.append({
                "type": "stderr",
                "payload": f"[update_cell error] {exc}",
            })
        finally:
            if task.done():
                self._inflight_emits.discard(task)
            else:
                task.add_done_callback(self._inflight_emits.discard)
        receipt = dict(getattr(self,"_last_file_receipt",{}))
        receipt.pop("refused", None)
        ack = {
            "op": "ack_update", "ok": error is None, "cell_id": version_id,
            "refused": refused or [],
            **receipt,
        }
        if error is not None:
            ack.update(cell_id="", error=error)
        await self._send_ack(ack, data)


    # ------------------------------------------------------------------
    # Lifecycle phase + progress
    # ------------------------------------------------------------------

    async def _op_phase(self, data: dict) -> None:
        """Set the run's lifecycle phase and its message. Fire-and-forget (no
        ack). ``gen.phase(name, message)`` sends both: a phase with a
        sub-message is the whole status concept, and the same phase with a
        new message only logs and broadcasts (:meth:`_apply_phase`). There is
        no separate 'status' op or event type; a frame without a
        generator-settable phase is refused.
        """
        from process_manager.phases import GENERATOR_PHASES
        requested = data.get("phase")
        if requested not in GENERATOR_PHASES:
            self.log_refusal("phase", f"{requested!r} is not a generator-settable phase")
            return
        self._apply_phase(requested, data.get("message") or "")

    async def _op_progress(self, data: dict) -> None:
        """``gen.progress(current, total)`` — a live progress counter. Fire-
        and-forget; rendered as a PROG line in the canonical log."""
        self.log_buffer.append({
            "type": "progress",
            "payload": json.dumps({
                "current": data.get("current"),
                "total": data.get("total"),
            }),
        })

    # ------------------------------------------------------------------
    # Host-service ops
    # ------------------------------------------------------------------


    async def _op_surface_register(self, data: dict) -> None:
        """Register a run's inbox: a pointer surface, or its transport-only
        inbox (broadcast when several viewers may attach)."""
        ack_body: dict = {"op": "ack_surface_register"}
        try:
            from served_surfaces import register, SurfaceTaken
            from sessions.chat import InboxExists
            try:
                label = data.get("label")
                if not isinstance(label, str):
                    raise ValueError("invalid_surface_declaration")
                result = register(self, data.get("path", ""), label=label,
                    transport_only=data.get("transport_only", False),
                    broadcast=data.get("broadcast", False))
            except InboxExists:
                raise ValueError("inbox_exists") from None
            ack_body.update(result, ok=True)
        except SurfaceTaken as exc:
            ack_body.update(ok=False, **exc.response())
        except Exception as exc:
            ack_body.update(ok=False, error=str(exc))
        await self._send_ack(ack_body, data)

    async def _op_chat_poll(self, data: dict) -> None:
        """Non-blocking pop of the next inbound renderer command (user_msg /
        interrupt) for the run's chat session.

        Before handing the generator a turn-triggering message, flush the
        user's pending Files-tab edits into the workspace + version chain so
        the generator reads them on this turn."""
        command = None
        try:
            from sessions.chat import pop_command
            command = pop_command(self.run_id)
        except Exception:
            command = None
        if command and command.get("kind") == "user_msg":
            # While a turn is streaming (background turns keep running across
            # thread switches) the tip layer waits for the turn to settle: a
            # change someone else committed would otherwise land under the
            # busy turn's in-progress files. Explicit user edits (drafts)
            # still land.
            include_tip = True
            try:
                from sessions.chat import get_inbox
                sess = get_inbox(self.run_id)
                include_tip = not (sess and sess.in_progress_turn_id)
            except Exception:
                include_tip = True
            try:
                await asyncio.to_thread(
                    self._flush_chat_files_to_workspace, include_tip,
                )
            except Exception as exc:
                self.log_buffer.append({
                    "type": "stderr",
                    "payload": f"[chat draft flush] {exc}",
                })
        await self._send_ack({"op": "ack_chat_poll", "command": command}, data)







    @staticmethod
    def _read_paths(data: dict) -> set[str] | None:
        if "paths" not in data:
            return None
        paths = data["paths"]
        if not isinstance(paths, list):
            raise ValueError("paths must be a list of relative cell paths")
        from collimate.fs import relative_parts
        selected = set()
        for path in paths:
            if not isinstance(path, str) or "\\" in path or "\0" in path:
                raise ValueError("invalid cell path")
            selected.add("/".join(relative_parts(path)))
        return selected




    async def _handler_request(self, data: dict, fn, key: str, *args) -> None:
        reply: dict = {"op": f"ack_{data['op']}"}
        try:
            reply[key] = await asyncio.to_thread(fn, self, *args)
        except Exception as exc:
            reply.update(ok=False, error=str(exc))
        await self._send_ack(reply, data)

    async def _op_list_handlers(self, data: dict) -> None:
        from handler_runtime import public_handlers
        await self._handler_request(data, public_handlers, "handlers")

    async def _op_handler_skills(self, data: dict) -> None:
        from handler_runtime import handler_skills
        await self._handler_request(data, handler_skills, "skills", data.get("ext_or_id"))

    async def _op_validate_file(self, data: dict) -> None:
        from handler_runtime import validate_workspace
        await self._handler_request(data, validate_workspace, "result", data.get("path"), data.get("verifier"))

    async def _op_read_cell_hashes(self, data: dict) -> None:
        reply = {"op":"ack_read_cell_hashes","ok":True,"hashes":{}}
        try:
            paths = self._read_paths(data)
            reply["hashes"] = await asyncio.to_thread(self._compute_cell_live_hashes, paths)
        except Exception as exc:
            reply.update(ok=False, error=str(exc))
        await self._send_ack(reply, data)

    async def _op_read_cell_files(self, data: dict) -> None:
        """Read accepted typed bytes with their exact revision identities."""
        def read():
            import base64
            from cas import read_blob
            paths=self._read_paths(data)
            values=self._cell_live_metadata(paths,data.get('revisions'))
            result={}
            for path,entry in values.items():
                raw=read_blob(entry['body_hash'])
                if raw is None:
                    raise OSError('Missing accepted file '+path)
                result[path]={'content':base64.b64encode(raw).decode('ascii'),
                              'body_hash':entry['body_hash'],'file_revision':entry['file_revision'],'encoding':entry['encoding']}
            return result

        ack_body: dict = {"op":"ack_read_cell_files","ok":True,"files":{}}
        try:
            ack_body["files"] = await asyncio.to_thread(read)
        except Exception as exc:
            ack_body.update(ok=False, error=str(exc))
        await self._send_ack(ack_body, data)

    async def _op_port_bound(self, data: dict) -> None:
        """``gen.serve_port`` — a port the reverse proxy may forward to for
        this run.

        The proxy trusts what this records, so the host records only what it
        can see: the port must be one this run serves (``serves_port``).
        Anything else is refused with a NAK and logged, and nothing is kept."""
        port = data.get("port")
        reason = ""
        if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
            reason = f"invalid_port: {port!r}"
        elif not await asyncio.to_thread(self.serves_port, port):
            reason = f"port_not_exposed: {port}"
        if reason:
            self.log_refusal("port_bound", reason)
            await self._send_nak("port_bound", reason, req_id=data["req_id"])
            return
        self.bound_ports.add(port)
        self.log_buffer.append({"type": "port_bound", "payload": json.dumps(data)})
        await self._send_ack({"op": "ack_port_bound", "ok": True}, data)

    async def _op_create_terminal_session(self, data: dict) -> None:
        ack_body: dict = {"op": "ack_create_terminal_session"}
        try:
            ref = str(data.get("container_id") or "")
            container = self.owned_container(ref)
            if container is None:
                raise ContainerNotOwned(
                    f"container {ref!r} is not one of this run's environments"
                )
            from sessions.terminal import register_session
            result = register_session(
                run_id=self.run_id,
                cell_id=self.cell_id or "",
                container_id=container.id,
                command=list(data.get("command") or []),
                cwd=data.get("cwd") or None,
                env=data.get("env") or {},
                keep_alive=bool(data.get("keep_alive")),
            )
            ack_body.update(result)
            ack_body["ok"] = True
        except ContainerNotOwned as exc:
            self.log_refusal("create_terminal_session", str(exc))
            ack_body.update(ok=False, reason="not_owned", error=str(exc))
        except Exception as exc:
            ack_body["ok"] = False
            ack_body.update(ok=False, error=str(exc))
        await self._send_ack(ack_body, data)

    def host_terminal_refusal(self, cwd: str) -> str:
        """Why this run may not open a terminal on this machine in ``cwd``,
        or ``""``. The run's effective grant must allow execution here —
        docker none or full (limited confines the run to one container),
        commands not deny (nothing holds a deny on an interactive TTY) — a
        run bound to a harness must have it placed with its CLI on this
        machine (the run's placement record: the terminal surface, runtime
        ``host``), and ``cwd`` must be a directory of the run's workspace."""
        grant = self.permissions
        if grant.docker == "limited":
            return "docker: limited confines the run to one container"
        if grant.commands == "deny":
            return "commands: deny cannot be held on an interactive terminal on this machine"
        placement = getattr(self, "placement", None)
        if placement is not None:
            if placement.get("surface") != "terminal":
                return f"the run's surface is {placement.get('surface')!r}, not its harness's CLI"
            if placement.get("runtime") != "host":
                return (f"the run's placement puts the {placement.get('harness')} CLI in its "
                        "container, not on this machine")
        workspace = os.path.realpath(str(getattr(self, "workspace_dir", "") or ""))
        real = os.path.realpath(cwd) if cwd else ""
        if not workspace or not real or os.path.commonpath([workspace, real]) != workspace:
            return f"cwd {cwd!r} is not a directory of the run's workspace"
        return ""

    async def _op_create_host_terminal_session(self, data: dict) -> None:
        """``gen.start_terminal(None, …)`` — a PTY running ``command`` on this
        machine. Gated here, not by a capability: the grant check and the
        run's harness placement (:meth:`host_terminal_refusal`); a refusal
        is logged with its reason, like a container the run does not own."""
        ack_body: dict = {"op": "ack_create_host_terminal_session"}
        try:
            cwd = str(data.get("cwd") or "")
            reason = await asyncio.to_thread(self.host_terminal_refusal, cwd)
            if reason:
                raise HostTerminalRefused(reason)
            from sessions.terminal import register_session
            result = register_session(
                run_id=self.run_id,
                cell_id=self.cell_id or "",
                container_id=None,
                command=list(data.get("command") or []),
                cwd=os.path.realpath(cwd),
                env=data.get("env") or {},
                keep_alive=bool(data.get("keep_alive")),
                locus="host",
            )
            ack_body.update(result)
            ack_body["ok"] = True
        except HostTerminalRefused as exc:
            self.log_refusal("create_host_terminal_session", str(exc))
            ack_body.update(ok=False, reason="host_refused", error=str(exc))
        except Exception as exc:
            ack_body.update(ok=False, error=str(exc))
        await self._send_ack(ack_body, data)

    async def _op_terminal_status(self, data: dict) -> None:
        """``gen.terminal_status(terminal_id)`` — the PTY's liveness, so a
        generator serving a terminal can end its leg when the program
        exits rather than park on a dead session."""
        ack_body: dict = {"op": "ack_terminal_status"}
        try:
            from sessions.terminal import session_status
            ack_body.update(session_status(data.get("terminal_id", "") or "",
                                           run_id=self.run_id))
            ack_body["ok"] = True
        except Exception as exc:
            ack_body["ok"] = False
            ack_body.update(ok=False, error=str(exc))
        await self._send_ack(ack_body, data)

    async def _op_destroy_terminal_session(self, data: dict) -> None:
        try:
            from sessions.terminal import unregister_session
            unregister_session(data.get("terminal_id", "") or "", run_id=self.run_id)
        except Exception as exc:
            self.log_buffer.append({
                "type": "stderr",
                "payload": f"[destroy_terminal_session error] {exc}",
            })

    async def _op_poll_renderer_action(self, data: dict) -> None:
        """Pop the first pending renderer action matching the caller's
        filters (``source`` / ``action_id`` — empty matches anything), FIFO
        within the match. Filters are what let several concurrent waiters
        (a chat card, a terminal button loop) share the queue
        without stealing each other's answers (agent-v2 §4.3.4). Non-matching
        entries stay queued for their own waiter."""
        want_source = str(data.get("source", "") or "")
        want_id = str(data.get("action_id", "") or "")
        picked = None
        for i, entry in enumerate(self._pending_renderer_actions):
            if want_source and entry.get("source", "") != want_source:
                continue
            if want_id and entry.get("action_id", "") != want_id:
                continue
            picked = self._pending_renderer_actions.pop(i)
            break
        ack: dict = {"op": "ack_poll_renderer_action",
                     "action_id": (picked or {}).get("action_id")}
        if picked is not None:
            ack["source"] = picked.get("source", "")
            if picked.get("payload") is not None:
                ack["payload"] = picked["payload"]
        await self._send_ack(ack, data)

    async def _op_stream_event(self, data: dict) -> None:
        """Streaming turn_start / text_delta / tool / turn_end / error events
        from the generator. Fire-and-forget — no ack. Routed to THIS run's
        inbox session, which forwards them over the renderer WebSocket. The
        canonical transcript is the run's own file; these deltas are the
        live overlay."""
        from sessions.chat import push_event
        push_event(self.run_id, data.get("turn_id", ""), data.get("event", {}))

    async def _op_reveal_file(self, data: dict) -> None:
        """``gen.reveal_file(path)`` — show one file of the run's cell to the
        user (the tab opens when the path is in the cell). Fire-and-forget.
        The path must be in the cell's committed tip: the SDK flushes a
        pending write before it asks."""
        path = str(data.get("path", "") or "")
        try:
            present = await asyncio.to_thread(self._committed_path, path)
        except Exception as exc:
            self.log_refusal("reveal_file", str(exc))
            return
        if not present:
            self.log_refusal("reveal_file", f"{path!r} is not a file of the cell")
            return
        from events import broadcast
        from collimate._host.channel_events import RevealFile
        broadcast(self.channel, RevealFile(self.run_id, self.cell_id or "", path).to_dict())

    def _committed_path(self, path: str) -> bool:
        if not path or not self.cell_id:
            return False
        from versions import find_tip
        conn = get_db_connection(self.channel)
        try:
            tip = find_tip(conn, self.cell_id)
            return path in version_file_hashes(conn, tip, [path])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # The run's annotation context
    #
    # Every refusal is an ack ``{ok: false, error}``, never a NAK, so the SDK
    # tells a refusal from a protocol failure. A remembered operation (a
    # pull in this manager's memory, a post durably) replays its answer
    # before the run's liveness is checked.
    # ------------------------------------------------------------------

    def _annotations_live(self) -> frozenset:
        """The run ids whose context accepts writes: this run while it is
        live, nothing once it is sealed or stopping."""
        if self.sealed or self.stop_requested or self.final_status:
            return frozenset()
        return frozenset({self.run_id})

    def _annotations_txn(self, work):
        """Run ``work(conn)`` in one write transaction on the channel db."""
        conn = get_db_connection(self.channel)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = work(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _annotations_read(self, work):
        conn = get_db_connection(self.channel)
        try:
            return work(conn)
        finally:
            conn.close()

    def _ring(self, bells) -> None:
        """Broadcast one ``annotations_changed`` per doorbell."""
        from collimate._host.channel_events import AnnotationsChanged
        from events import broadcast
        for bell in bells:
            try:
                broadcast(self.channel, AnnotationsChanged(bell.cell_id, bell.context_id, bell.rev).to_dict())
            except Exception:
                _logger.debug("annotations_changed broadcast failed", exc_info=True)

    @staticmethod
    def _annotation_refusal(op: str, exc) -> dict:
        code = "reply_depth" if exc.code == "parent_not_root" else exc.code
        return {"op": f"ack_{op}", "ok": False, "error": code, "message": exc.message}

    @staticmethod
    def _operation_id(data: dict) -> str | None:
        value = data.get("operation_id")
        if isinstance(value, str) and 1 <= len(value) <= _OPERATION_ID_MAX:
            return value
        return None

    async def _op_annotations_poll(self, data: dict) -> None:
        """What waits in the run's context: ``{rev, unread, interrupt_seq}``.
        The first poll marks the run listening; later polls only read."""
        from annotations import AnnotationRefused, mark_listening, notice
        op = "annotations_poll"
        try:
            if not self._annotations_live():
                raise AnnotationRefused("sealed")
            seen = await asyncio.to_thread(
                self._annotations_read, lambda c: notice(c, self.cell_id or "", self.run_id))
            if seen["sealed"]:
                raise AnnotationRefused("sealed")
            if not seen["listening"]:
                bells = await asyncio.to_thread(
                    self._annotations_txn, lambda c: mark_listening(c, self.cell_id or "", self.run_id))
                self._ring(bells)
                if bells:
                    seen["rev"] = bells[-1].rev
                    from routes.system import broadcast_runs_active_changed
                    broadcast_runs_active_changed(self.channel)
            ack = {"op": f"ack_{op}", "ok": True, "rev": seen["rev"], "unread": seen["unread"],
                   "interrupt_seq": seen["interrupt_seq"], "listening": True}
        except AnnotationRefused as exc:
            ack = self._annotation_refusal(op, exc)
        await self._send_ack(ack, data)

    async def _op_annotations_pull(self, data: dict) -> None:
        """Take the run's unread messages, marking them read (the user's
        pending drafts flush to the workspace first, as for a chat turn),
        with the context's ``{rev, unread, interrupt_seq}`` as the pull left
        it, read in the same transaction. The receipt is remembered by
        ``operation_id`` so a retry replays it."""
        from annotations import AnnotationRefused, notice, pull
        op = "annotations_pull"
        try:
            operation_id = self._operation_id(data)
            if operation_id is None:
                raise AnnotationRefused("invalid", "A pull carries its operation id.")
            remembered = self._annotation_pulls.get(operation_id)
            if remembered is not None:
                await self._send_ack({"op": f"ack_{op}", "ok": True, "operation_id": operation_id,
                                      **remembered}, data)
                return
            if not self._annotations_live():
                raise AnnotationRefused("sealed")
            include_tip = True
            try:
                from sessions.chat import get_inbox
                inbox = get_inbox(self.run_id)
                include_tip = not (inbox and inbox.in_progress_turn_id)
            except Exception:
                include_tip = True
            try:
                await asyncio.to_thread(self._flush_chat_files_to_workspace, include_tip)
            except Exception as exc:
                self.log_buffer.append({"type": "stderr", "payload": f"[annotations draft flush] {exc}"})
            def take(conn):
                messages, bells = pull(conn, self.cell_id or "", self.run_id, live=self._annotations_live())
                left = notice(conn, self.cell_id or "", self.run_id)
                return {"messages": messages, "rev": left["rev"], "unread": left["unread"],
                        "interrupt_seq": left["interrupt_seq"]}, bells

            receipt, bells = await asyncio.to_thread(self._annotations_txn, take)
            self._annotation_pulls[operation_id] = receipt
            while len(self._annotation_pulls) > ANNOTATION_PULL_RECEIPTS:
                self._annotation_pulls.popitem(last=False)
            self._ring(bells)
            ack = {"op": f"ack_{op}", "ok": True, "operation_id": operation_id, **receipt}
        except AnnotationRefused as exc:
            ack = self._annotation_refusal(op, exc)
        await self._send_ack(ack, data)

    async def _op_annotations_post(self, data: dict) -> None:
        """The run's own message into its context (author 'run', its label,
        born read); ``reply_to`` answers a top-level message. The same
        operation id replays its stored answer."""
        from annotations import AnnotationRefused, request_digest, run_post
        op = "annotations_post"
        try:
            operation_id = self._operation_id(data)
            if operation_id is None:
                raise AnnotationRefused("invalid", "A post carries its operation id.")
            reply_to = data.get("reply_to") or None
            if reply_to is not None and not isinstance(reply_to, str):
                raise AnnotationRefused("invalid", "reply_to names a message id.")
            message_id, body = data.get("message_id"), data.get("body")
            digest = request_digest({"cell_id": self.cell_id or "", "context_id": self.run_id,
                                     "message_id": message_id, "body": body, "reply_to": reply_to})
            message, bells = await asyncio.to_thread(
                self._annotations_txn,
                lambda c: run_post(c, self.cell_id or "", self.run_id, label=self.label,
                                   message_id=message_id, body=body, reply_to=reply_to,
                                   operation_id=operation_id, digest=digest,
                                   live=self._annotations_live()))
            self._ring(bells)
            ack = {"op": f"ack_{op}", "ok": True, "operation_id": operation_id, "id": message["id"]}
        except AnnotationRefused as exc:
            ack = self._annotation_refusal(op, exc)
        await self._send_ack(ack, data)

    async def _op_annotations_list(self, data: dict) -> None:
        """The run's whole context as the run sees it (no held messages)."""
        from annotations import AnnotationRefused, list_for_run
        op = "annotations_list"
        try:
            messages = await asyncio.to_thread(
                self._annotations_read, lambda c: list_for_run(c, self.cell_id or "", self.run_id))
            ack = {"op": f"ack_{op}", "ok": True, "messages": messages}
        except AnnotationRefused as exc:
            ack = self._annotation_refusal(op, exc)
        await self._send_ack(ack, data)

    # ------------------------------------------------------------------
    # Client capabilities

    async def _op_client_caps(self, data: dict) -> None:
        """What the client driving this server can do (gen.client_caps).
        Server-lifetime state, not channel-scoped — one server is driven by
        one client — so this reads the registry directly with no thread hop."""
        from client_caps import client_caps
        await self._send_ack({"op": "ack_client_caps", "caps": client_caps()}, data)


    async def _op_account_materialize(self, data: dict) -> None:
        from accounts import materialize_cli_account
        ack = {"op": "ack_account_materialize", "ok": False}
        try:
            role = data.get("role", "")
            account = getattr(self, "account_context", {}).get(role)
            if account is None:
                raise ValueError(f"No account selected for {role!r}")
            lease = await asyncio.to_thread(materialize_cli_account, account, self.run_id)
            leases = getattr(self, "_account_leases", None)
            if leases is None:
                leases = self._account_leases = {}
            leases[lease["lease_id"]] = role
            bindings = getattr(self, "host_bindings", None)
            if bindings is None:
                bindings = self.host_bindings = {}
            bindings[lease["lease_id"]] = {"path": os.path.realpath(lease["path"]), "mode": "rw"}
            ack.update(ok=True, **lease)
        except Exception as exc:
            ack["error"] = str(exc)
        await self._send_ack(ack, data)

    async def _op_account_release(self, data: dict) -> None:
        from accounts import release_cli_account
        ack = {"op": "ack_account_release", "ok": False}
        try:
            role, lease_id = data.get("role", ""), data.get("lease_id", "")
            if getattr(self, "_account_leases", {}).get(lease_id) != role:
                raise ValueError("Account lease does not belong to this run and role")
            try:
                result = await asyncio.to_thread(release_cli_account, lease_id, self.run_id)
            finally:
                # The account store retires the lease even when collection
                # fails and preserves its files for the next startup sweep.
                self._account_leases.pop(lease_id, None)
                getattr(self, "host_bindings", {}).pop(lease_id, None)
            ack.update(ok=True, **result)
        except Exception as exc:
            ack["error"] = str(exc)
        await self._send_ack(ack, data)

    async def _op_account(self, data: dict) -> None:
        import copy
        role = data.get("role", "")
        account = getattr(self, "account_context", {}).get(role)
        declared = role in getattr(self, "account_requirements", {})
        ack = {"op": "ack_account", "ok": False}
        if account is None:
            ack.update(account=None, reason="not_found" if declared else "permission_denied",
                       error=f"No account selected for {role!r}")
        else:
            ack.update(ok=True, account=copy.deepcopy(account))
        await self._send_ack(ack, data)

    async def _op_commit_account(self, data: dict) -> None:
        from accounts import commit_refreshed_secrets
        role = data.get("role", "")
        account = getattr(self, "account_context", {}).get(role)
        ack = {"op": "ack_commit_account", "ok": False}
        try:
            if account is None:
                raise ValueError("No account selected for this role")
            revision = account.get("credential_revision", account["revision"])
            owner = account.get("credential_account_id", account["id"])
            if data.get("expected_revision") != revision:
                raise ValueError("Account snapshot does not match this run")
            secrets = data.get("secrets") or {}
            result = await asyncio.to_thread(commit_refreshed_secrets, owner, revision, secrets)
            # A global edit/deletion may block persistence; this run still
            # keeps the refresh it just performed, with its original metadata.
            for snapshot in self.account_context.values():
                if snapshot.get("credential_account_id", snapshot["id"]) == owner:
                    snapshot["secrets"] = {**snapshot.get("secrets", {}), **secrets}
                    if result:
                        snapshot["credential_revision"] = result
                        if snapshot["id"] == owner:
                            snapshot["revision"] = result
            ack.update(ok=True, committed=bool(result), revision=result)
        except Exception as exc:
            ack["error"] = str(exc)
        await self._send_ack(ack, data)


    # ------------------------------------------------------------------
    @property
    def environment_service(self):
        """Lazy per-run EnvironmentService; created on first env_place."""
        service = getattr(self, "_environment_service", None)
        if service is None:
            from process_manager.environments import EnvironmentService
            service = EnvironmentService(self)
            self._environment_service = service
        return service

    def owned_container(self, ref: str):
        """The container of one of this run's open environments that ``ref``
        names (its id or name), or None. The ownership record behind every
        host action on a container the run names: a snapshot's archive read,
        a terminal's ``docker exec``."""
        service = getattr(self, "_environment_service", None)
        return service.owned_container(ref) if service is not None else None

    def serves_port(self, port: int) -> bool:
        """Whether this run serves loopback ``port``: one of its environments
        publishes or forwards it, or its execution backend sees a process of
        the run listening on it. Blocking (the backend may read the process
        table)."""
        service = getattr(self, "_environment_service", None)
        if service is not None and service.serves_port(port):
            return True
        backend = getattr(self, "backend", None)
        return backend is not None and backend.serves_port(self, port)

    def log_refusal(self, action: str, reason: str) -> None:
        """Record a claim the host refused in the run's log, naming the run."""
        self.log_buffer.append({
            "type": "ipc_error",
            "payload": f"run {self.run_id}: {action} refused: {reason}",
        })

    async def _op_env_place(self, data: dict) -> None:
        try:
            placed = await asyncio.to_thread(
                self.environment_service.place, dict(data.get("spec") or {})
            )
            await self._send_ack({"op": "ack_env_place", "ok": True, "error": None, **placed}, data)
        except Exception as exc:
            await self._send_ack({
                "op": "ack_env_place", "ok": False,
                "env_id": "", "container_id": "", "error": str(exc),
            }, data)

    async def _op_env_exec(self, data: dict) -> None:
        try:
            rc, output = await asyncio.to_thread(
                self.environment_service.exec,
                str(data.get("env_id") or ""),
                data.get("command"),
                cwd=str(data.get("cwd") or ""),
                env=dict(data.get("env") or {}),
                user=str(data.get("user") or ""),
                detach=bool(data.get("detach")),
            )
            await self._send_ack({
                "op": "ack_env_exec", "ok": True, "rc": rc, "output": output,
                "error": None,
            }, data)
        except Exception as exc:
            await self._send_ack({
                "op": "ack_env_exec", "ok": False, "rc": -1, "output": "",
                "error": str(exc),
            }, data)

    async def _op_env_expose_port(self, data: dict) -> None:
        try:
            host_port = await asyncio.to_thread(
                self.environment_service.expose_port,
                str(data.get("env_id") or ""),
                int(data.get("port") or 0),
            )
            await self._send_ack({
                "op": "ack_env_expose_port", "ok": True,
                "host_port": host_port, "error": None,
            }, data)
        except Exception as exc:
            await self._send_ack({
                "op": "ack_env_expose_port", "ok": False,
                "host_port": 0, "error": str(exc),
            }, data)

    async def _op_env_close(self, data: dict) -> None:
        try:
            await asyncio.to_thread(
                self.environment_service.close, str(data.get("env_id") or "")
            )
            await self._send_ack({"op": "ack_env_close", "ok": True, "error": None}, data)
        except Exception as exc:
            await self._send_ack({"op": "ack_env_close", "ok": False, "error": str(exc)}, data)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _op_finish(self, data: dict) -> None:
        """Generator's clean shutdown signal. Ends the run row (status,
        ``ended_at``, and the run's own ``note`` / ``outcome`` when the
        generator sent them) and broadcasts the terminal status. Run exit
        without finish (e.g. SIGKILL) is handled by _wait_and_cleanup →
        _finalize_run_status."""
        success = data.get("success", True)
        status = "completed" if success else "failed"
        error_msg = data.get("error_message", "")
        note = data.get("note")
        note = note if isinstance(note, str) else None
        outcome = data.get("outcome")
        if outcome is not None and (not isinstance(outcome, str) or outcome not in FINISH_OUTCOMES):
            self.log_buffer.append({
                "type": "ipc_error",
                "payload": f"finish: outcome must be 'pass' or 'fail', not {outcome!r}",
            })
            outcome = None
        self.log_buffer.append({
            "type": "finish",
            "payload": json.dumps({"success": success, "error_message": error_msg}),
        })
        # A stopping run's status belongs to its stopper: the stop route and
        # Stop all write 'stopped', a watchdog timeout is finalized
        # 'timed_out'. The SDK's SIGTERM path sends finish(False, 'Stopped by
        # user'), which must not overwrite or pre-empt either.
        if getattr(self, "stop_requested", False) or getattr(self, "timeout_reason", None):
            return
        failure_reason = (error_msg or "").strip() or None
        # Stays on the loop: finish must commit before _finalize_run_status's
        # guarded UPDATE runs, or a generator that reports failure but exits
        # 0 would be finalized 'completed'.
        conn = get_db_connection(self.channel)
        try:
            changed = mark_run_ended(conn, self.run_id, status=status,
                                     failure_reason=failure_reason,
                                     note=note, outcome=outcome)
            conn.commit()
            row = conn.execute(
                "SELECT status, failure_reason FROM runs WHERE id=?", (self.run_id,)
            ).fetchone()
        finally:
            conn.close()
        if changed and row:
            self._broadcast_run_status(row["status"], row["failure_reason"])

    # ------------------------------------------------------------------
    # Fall-through (unknown ops)
    # ------------------------------------------------------------------

    async def _unknown_op(self, data: dict) -> None:
        """Handle an op without a dedicated handler: surface it as an
        ipc_error so it's visible, and still persist the raw frame for
        debugging under the fixed type ``ipc_unknown_op`` — never under the
        op name, which would let a generator forge a rendered or modal event
        type. The host acts on nothing it carries."""
        op = data.get("op")
        self.log_buffer.append({
            "type": "ipc_error",
            "payload": f"unknown IPC op: {op!r}",
        })
        self.log_buffer.append({"type": "ipc_unknown_op", "payload": json.dumps(data)})

    def _broadcast_dag_commit(self, cell_id: str, files: list[str]):
        """Broadcast DAG commit + state update to SSE subscribers.

        The ``dag_commit`` message is small and latency-sensitive (it drives
        FILES_CHANGED fan-out to mounted renderers), so it goes out
        immediately. Coalesced state transport follows the channel stream's
        snapshot/delta contract, so a commit burst shares its state work
        without delaying the latency-sensitive file notification.

        ``cell_id`` must be the STABLE canvas cell id — the one the frontend
        keys its mounted renderers on. A freshly minted version row is not
        a canvas cell and must never be used as the broadcast target."""
        from cell_commits import broadcast_cell_commit
        broadcast_cell_commit(self.channel, cell_id,
                              self.run_id, self.generator_name, files=files)
