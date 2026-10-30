# SPDX-License-Identifier: MIT
"""Token-authenticated, fixed-table IPC relay for command-launched generators.

The parent owns this listener. The caller launches the child in its command
environment, never in the parent's Python process. This module neither starts
a runner nor acquires an environment/account on the child's behalf.
"""
from __future__ import annotations

import hmac
import json
import os
import posixpath
import secrets
import shutil
import socket
import socketserver
import threading
from dataclasses import dataclass

from collimate._host.ipc import FINISH_OUTCOMES, IPC_MAX_LINE_BYTES, finish_note


# The annotation ops are not relayed: a child has no annotation context
# (its gen.annotations.available is False).
FORWARDED_OPS = {
    "update_cell": "ack_update",
    "read_cell_files": "ack_read_cell_files",
    "reveal_file": None,
    "client_caps": "ack_client_caps",
    "list_handlers": "ack_list_handlers",
    "handler_skills": "ack_handler_skills",
    "validate_file": "ack_validate_file",
}
LOCAL_OPS = frozenset({"finish", "phase", "progress", "port_bound"})


@dataclass(frozen=True)
class PreparedChild:
    """Command and private configuration staged without executing draft code."""

    argv: list[str]
    cwd: str
    env: dict[str, str]
    run_dir: str
    host_run_dir: str

    def close(self):
        if os.path.islink(self.host_run_dir):
            os.unlink(self.host_run_dir)
        else:
            shutil.rmtree(self.host_run_dir, ignore_errors=True)


def prepare_child(relay: "Forwarder", path: str, *, settings: dict | None = None,
                  prompt: str = "", selected_file: str | None = None,
                  child_workspace: str | None = None,
                  ipc_host: str = "127.0.0.1", sdk_path: str | None = None) -> PreparedChild:
    """Stage a draft package and return its ``uv run`` command for the caller.

    ``path`` is the cell-relative .generator definition. ``selected_file``
    is the cell-relative path the child reads as ``gen.selected_file``.
    ``child_workspace``
    is the same workspace as mounted in the command environment. The caller
    supplies an SDK installation path visible there when needed; Python's
    environment otherwise finds the SDK installed in the command image.
    This function parses declarations only; the command environment executes
    the entrypoint and its inline dependency declarations.
    """
    import yaml
    from collimate import fs
    from collimate.shared import sdk_distribution
    from collimate.config_values import parse_set_yaml, resolve_generator_values
    from collimate.pointers import is_run_path

    def relative(value):
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("test generator paths must be relative")
        parts = fs.relative_parts(value)
        if "/".join(parts) != value or is_run_path(value) or parts[0] == ".collrun":
            raise ValueError("test generator paths must name ordinary cell files")
        return value

    relative(path)
    if not path.endswith(".generator") or "/" not in path:
        raise ValueError("path must name a packaged .generator definition")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be text")
    if selected_file is not None:
        relative(selected_file)
    bridge = relay.bridge
    workspace = bridge.workspace
    child_workspace = child_workspace or workspace
    package = posixpath.dirname(path)

    def read(name):
        with os.fdopen(fs.open_file(workspace, name, write=False), "r", encoding="utf-8") as handle:
            return handle.read()

    manifest = yaml.safe_load(read(path))
    if not isinstance(manifest, dict):
        raise ValueError("generator definition must be an object")
    entrypoint = relative(manifest.get("entrypoint"))
    # Open once with no-follow traversal before returning any command. The child
    # still operates in its existing command boundary if the file changes later.
    with os.fdopen(fs.open_file(workspace, f"{package}/{entrypoint}", write=False), "rb"):
        pass
    main_config = manifest.get("main_config")
    if settings and not main_config:
        raise ValueError("generator has no main_config for these settings")
    run_relative = f".collrun/forward-{secrets.token_hex(16)}"
    host_run_dir = os.path.join(workspace, run_relative)
    run_dir = posixpath.join(child_workspace, run_relative)
    staged = {}
    for name in manifest.get("draft_files") or []:
        relative(name)
        staged[name] = read(f"{package}/{name}")
    if main_config:
        relative(main_config)
        raw = staged.get(main_config)
        if raw is None:
            raw = read(f"{package}/{main_config}")
        values = {**resolve_generator_values(parse_set_yaml(raw)), **(settings or {})}
        staged[main_config] = yaml.safe_dump({"values": values}, sort_keys=False)
    runner = {"runner": bridge.runner_id, "name": bridge.runner_name,
              "label": bridge.label, "folder": bridge.folder}
    env = {
        "COLLIMATE_IPC_PORT": str(relay.port), "COLLIMATE_IPC_TOKEN": relay.token,
        "COLLIMATE_IPC_HOST": ipc_host, "COLLIMATE_WORKSPACE": child_workspace,
        "COLLIMATE_IPC_OPS": json.dumps(sorted(set(FORWARDED_OPS) | LOCAL_OPS)),
        "COLLIMATE_RUN_DIR": run_dir, "COLLIMATE_RUN_ID": bridge.run_id,
        "COLLIMATE_CHANNEL": bridge.channel, "COLLIMATE_CELL_ID": bridge.cell_id,
        "COLLIMATE_RUNNER": json.dumps(runner),
        "COLLIMATE_PERMISSIONS": json.dumps(bridge.permissions),
        "COLLIMATE_PACKAGES": json.dumps(bridge.packages),
        "COLLIMATE_UI_FILES": json.dumps(manifest.get("ui_files") or []),
        "COLLIMATE_LIVE_FILES_MODE": str(manifest.get("live_files_mode") or "manual"),
    }
    if sdk_path is not None:
        env["PYTHONPATH"] = sdk_path
    # The SDK source can travel independently of uv's dependency environment;
    # its ordinary runtime dependencies still need to be present in that env.
    requirements = [value for value in (sdk_distribution().requires or []) if "extra ==" not in value]
    optional = manifest.get("optional_dependencies") or {}
    selector = optional.get("selector") or {}
    if selector.get("file") in staged:
        value = resolve_generator_values(parse_set_yaml(staged[selector["file"]])).get(selector.get("key"))
        key = str(value).lower() if isinstance(value, bool) else str(value or "").strip()
        selected = (optional.get("groups") or {}).get(key) or []
        if not isinstance(selected, list) or any(not isinstance(req, str) for req in selected):
            raise ValueError("optional dependency group must be a list of requirements")
        requirements.extend(selected)
    argv = ["uv", "run", "--no-project"]
    for requirement in dict.fromkeys(requirements):
        argv += ["--with", requirement]
    argv.append(posixpath.join(child_workspace, package, entrypoint))
    try:
        fs.write_file(workspace, f"{run_relative}/file_revisions.json",
                      json.dumps(bridge._persistence["revisions"]))
        fs.write_file(workspace, f"{run_relative}/prompt.md", prompt)
        if selected_file:
            fs.write_file(workspace, f"{run_relative}/selected_file", selected_file)
        for name, content in staged.items():
            fs.write_file(workspace, f"{run_relative}/config/files/{name}", content)
    except BaseException:
        shutil.rmtree(host_run_dir, ignore_errors=True)
        raise
    return PreparedChild(argv, posixpath.join(child_workspace, package), env, run_dir, host_run_dir)


class Forwarder:
    """Serve a child using its parent's bridge and identity.

    Bind to loopback for host command execution, or the Docker gateway address
    for a container command. The independent capability token is always
    required. ``result`` folds local lifecycle/status operations into the tool
    result. Exiting closes owned sockets; it never closes the parent's bridge.
    """

    def __init__(self, bridge=None, *, bind_host: str = "127.0.0.1"):
        if bridge is None:
            from collimate import native
            bridge = native.require_bridge()
        self.bridge = bridge
        self.bind_host = bind_host
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.result: dict = {"success": None}
        self._finished = threading.Event()
        self._lock = threading.RLock()
        self._sockets: set[socket.socket] = set()
        self._server = None
        self._thread = None
        self._closed = False

    def dispatch(self, frame: dict) -> dict | None:
        """Relay a parsed frame, replacing child request IDs via the bridge.

        ``HostBridge.emit_and_wait`` mints a fresh ID while holding the parent's
        request lock. Only the returned reply gets the child's original ID.
        """
        op = frame.get("op")
        req_id = frame.get("req_id")
        refusal = {"op": "nak", "for_op": op, "req_id": req_id,
                   "reason": "forward_refused"}
        with self._lock:
            if self._closed or self.result["success"] is not None:
                return refusal
            if op in LOCAL_OPS:
                if op == "finish":
                    self.result["success"] = frame.get("success") is True
                    self.result["error_message"] = str(frame.get("error_message") or "")
                    # The run's own verdict, kept as the host keeps it: a
                    # note that is not text and an unknown outcome are dropped.
                    note, outcome = frame.get("note"), frame.get("outcome")
                    self.result["note"] = finish_note(note) if isinstance(note, str) else None
                    self.result["outcome"] = (outcome if isinstance(outcome, str)
                                              and outcome in FINISH_OUTCOMES else None)
                    self._finished.set()
                else:
                    self.result[op] = frame.get(op, {key: value for key, value in frame.items()
                                                   if key not in ("op", "req_id")})
                return {"op": f"ack_{op}", "req_id": req_id, "ok": True} if req_id else None
            if op not in FORWARDED_OPS:
                self.result["refused"] = {"op": op, "reason": "forward_refused"}
                return refusal
        payload = {key: value for key, value in frame.items() if key != "req_id"}
        try:
            wait_op = FORWARDED_OPS[op]
            if wait_op is None:
                self.bridge.emit(payload)
                return None
            reply = self.bridge.emit_and_wait(payload, wait_op=wait_op)
            return {**reply, "req_id": req_id}
        except PermissionError:
            return {**refusal, "reason": "permission_denied"}
        except Exception as exc:
            return {**refusal, "reason": "forward_failed", "message": str(exc)}

    def __enter__(self):
        if self._server is not None or self._closed:
            raise RuntimeError("Forwarder instances are single-use")
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                with relay._lock:
                    relay._sockets.add(self.request)
                try:
                    self.request.settimeout(5)
                    first = self.rfile.readline(4097)
                    if len(first) > 4096 or not first.endswith(b"\n"):
                        return
                    auth = json.loads(first)
                    if (not isinstance(auth, dict) or auth.get("op") != "__auth__"
                            or not isinstance(auth.get("token"), str)
                            or not hmac.compare_digest(auth["token"], relay.token)):
                        return
                    self.request.settimeout(None)
                    while not relay._closed:
                        raw = self.rfile.readline(IPC_MAX_LINE_BYTES + 1)
                        if not raw or len(raw) > IPC_MAX_LINE_BYTES or not raw.endswith(b"\n"):
                            return
                        frame = json.loads(raw)
                        if not isinstance(frame, dict) or not isinstance(frame.get("op"), str):
                            return
                        req_id = frame.get("req_id")
                        if req_id is not None and (not isinstance(req_id, str) or len(req_id) > 128):
                            return
                        reply = relay.dispatch(frame)
                        if reply is not None:
                            response = (json.dumps(reply, ensure_ascii=False) + "\n").encode()
                            if len(response) > IPC_MAX_LINE_BYTES:
                                return
                            self.wfile.write(response)
                            self.wfile.flush()
                except (OSError, ValueError):
                    return
                finally:
                    with relay._lock:
                        relay._sockets.discard(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False
            allow_reuse_address = False

            def __init__(self, *args):
                self.slots = threading.BoundedSemaphore(4)
                super().__init__(*args)

            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.slots.release()

        self._server = Server((self.bind_host, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def wait_finished(self, timeout: float | None = None) -> bool:
        """Wait for buffered finish delivery after the child process exits.

        A failed finish still returns True. Closing without a finish unblocks
        waiters and returns False; an exit code is not a substitute for IPC.
        """
        self._finished.wait(timeout)
        with self._lock:
            return self.result["success"] is not None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._finished.set()
            sockets = tuple(self._sockets)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __exit__(self, *exc):
        self.close()
