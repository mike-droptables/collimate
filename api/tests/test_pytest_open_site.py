# SPDX-License-Identifier: MIT
"""A local site the user opens from a cell (POST /cells/{cell_id}/sites).

The host finds the live run on the cell that serves the port — a listener
in the generator's group, in one of its host terminal sessions, or in a
container of one of its environments (forwarded to a loopback port, or
relayed with an exec per connection when it listens on the container's
own loopback) — binds the host port for the proxy, publishes
``_runs/<label>/site-<port>.website`` and reveals it. Real ProcessManagers
on real cells, real sockets and processes; the environment's container is
a stand-in whose bridge address is loopback, or one whose "exec" runs the
command on this machine (its TCP tables are this machine's, the bridge
address 127.0.0.2, where a server bound to 127.0.0.1 refuses).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import types
import uuid

import pytest

from collimate_server.database import get_db_connection

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:open-site:channel"))
OTHER_CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:open-site:other"))

_LISTENER = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
    "print(s.getsockname()[1], flush=True); time.sleep(60)\n"
)


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        from collimate_server.models import Channel
        from collimate_server.state import flush_persist_state, persist_state, state
        channel = Channel(id=uuid.UUID(CHANNEL), name="Sites")
        other = Channel(id=uuid.UUID(OTHER_CHANNEL), name="Elsewhere")
        state.channels = [channel, other]
        state.active_channel_id = channel.id
        get_db_connection(CHANNEL).close()
        get_db_connection(OTHER_CHANNEL).close()
        persist_state()
        flush_persist_state()
        yield client


@pytest.fixture
def cell(client):
    return client.post(f"/api/stacks/{CHANNEL}/create").json()["main_stacks"][-1]["cells"][0]["id"]


@pytest.fixture
def procs():
    """Processes started for a test, each its own session, killed after."""
    started: list[subprocess.Popen] = []

    def spawn(code: str, *, stdout=True) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE if stdout else None, text=True,
                                start_new_session=True)
        started.append(proc)
        return proc

    yield spawn
    for proc in started:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


@pytest.fixture
def live(tmp_path, monkeypatch, procs):
    """Register live runs on cells: a real ProcessManager whose generator is
    a real process (a listener unless ``listen`` is false) under the native
    backend, with the run row its versions reference."""
    import collimate_server.annotations as annotations
    import collimate_server.events as events
    import collimate_server.run_registry as registry
    from collimate_server.process_manager import ProcessManager
    from collimate_server.process_manager.backends import NativeSubprocessBackend
    from collimate_server.runner_permissions import UNRESTRICTED

    runs: dict = {}
    monkeypatch.setattr(registry, "active_managers", runs)
    reveals: list = []
    monkeypatch.setattr(events, "broadcast", lambda ch, ev: reveals.append((ch, ev))
                        if ev.get("type") == "reveal_file" else None)

    def start(cell_id: str, *, listen: bool = True, channel: str = CHANNEL):
        rid = str(uuid.uuid4())
        conn = get_db_connection(channel)
        try:
            conn.execute("INSERT INTO runs (id, prompt, status, runner_id) VALUES (?, 'x', 'running', 'synth')",
                         (rid,))
            annotations.create_run_context(conn, cell_id, rid)
            conn.commit()
        finally:
            conn.close()
        ws = tmp_path / rid
        ws.mkdir()
        mgr = ProcessManager(
            channel=channel, run_id=rid, app_dir=str(ws), workspace_dir=str(ws),
            entrypoint="x.py", generator_name="synth", cell_id=cell_id,
            runner_id="synth", runner_name="Synth", permissions=UNRESTRICTED,
        )
        mgr._ipc_writer = None
        mgr.ui_files = (".website",)
        mgr.backend = NativeSubprocessBackend()
        generator = procs(_LISTENER if listen else "import time; time.sleep(60)", stdout=listen)
        mgr.process = types.SimpleNamespace(pid=generator.pid, returncode=None)
        mgr.served_port = int(generator.stdout.readline()) if listen else None
        runs[rid] = mgr
        return mgr

    return types.SimpleNamespace(start=start, runs=runs, reveals=reveals)


def _files(cell_id: str) -> dict[str, str]:
    from collimate_server.cas import channel_store
    from collimate_server.persistence import snapshot_files
    with get_db_connection(CHANNEL) as conn:
        return {row["path"]: channel_store(CHANNEL).read_blob(row["body_hash"]).decode() for row in snapshot_files(conn, cell_id)}


def _versions(cell_id: str) -> int:
    with get_db_connection(CHANNEL) as conn:
        return conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]


def _open(client, cell_id, port, channel=CHANNEL):
    return client.post(f"/api/channels/{channel}/cells/{cell_id}/sites", json={"port": port})


def test_a_port_the_generators_group_listens_on_is_published_and_revealed(client, cell, live):
    run = live.start(cell)
    port = run.served_port
    r = _open(client, cell, port)
    assert r.status_code == 200, r.text
    path = f"_runs/{run.label}/site-{port}.website"
    url = f"/proxy/{CHANNEL}/{run.run_id}/{port}/"
    assert r.json() == {"run_id": run.run_id, "label": run.label, "path": path, "url": url}
    assert json.loads(_files(cell)[path]) == {
        "url": url, "directUrl": f"http://127.0.0.1:{port}/", "loadingCover": False, "buttons": []}
    assert port in run.bound_ports
    assert live.reveals == [(CHANNEL, {"type": "reveal_file", "run_id": run.run_id,
                                       "cell_id": cell, "path": path})]

    # Idempotent: the same pointer, no new version, revealed again.
    versions = _versions(cell)
    again = _open(client, cell, port)
    assert again.status_code == 200 and again.json() == r.json()
    assert _versions(cell) == versions
    assert len(live.reveals) == 2 and live.reveals[1] == live.reveals[0]


def test_the_run_that_serves_the_port_is_chosen_among_the_cells_runs(client, cell, live):
    idle = live.start(cell, listen=False)
    serving = live.start(cell)
    r = _open(client, cell, serving.served_port)
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == serving.run_id
    assert not idle.bound_ports


def test_a_port_a_host_terminal_session_of_the_run_listens_on_is_served(client, cell, live, procs):
    from collimate_server.process_signals import start_ticks
    run = live.start(cell, listen=False)
    terminal = procs(_LISTENER)
    port = int(terminal.stdout.readline())
    assert _open(client, cell, port).status_code == 404
    run._host_terminal_sessions[terminal.pid] = start_ticks(terminal.pid)
    r = _open(client, cell, port)
    assert r.status_code == 200, r.text
    assert r.json()["path"] == f"_runs/{run.label}/site-{port}.website"
    assert port in run.bound_ports


class _Container:
    """A placed container whose bridge address is loopback."""
    id = "c0ffee"
    name = "collimate-env"
    attrs = {"NetworkSettings": {"IPAddress": "127.0.0.1"}}

    def reload(self):
        pass

    def stop(self, timeout=None):
        pass

    def remove(self, force=False):
        pass


def test_a_port_a_container_of_the_run_listens_on_is_forwarded(client, cell, live):
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment
    run = live.start(cell, listen=False)
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", _Container())
    run._environment_service = service
    app = socket.socket()
    app.bind(("127.0.0.1", 0))
    app.listen()
    port = app.getsockname()[1]
    try:
        r = _open(client, cell, port)
        assert r.status_code == 200, r.text
        host_port = service._envs["env-1"].forwarded[port]
        assert host_port != port
        assert r.json()["url"] == f"/proxy/{CHANNEL}/{run.run_id}/{host_port}/"
        assert r.json()["path"] == f"_runs/{run.label}/site-{port}.website"
        pointer = json.loads(_files(cell)[r.json()["path"]])
        assert pointer["directUrl"] == f"http://127.0.0.1:{host_port}/"
        assert host_port in run.bound_ports
        # The forwarder reaches the container's port.
        with socket.create_connection(("127.0.0.1", host_port), timeout=5):
            accepted, _ = app.accept()
            accepted.close()
        # A second open reuses the forwarder rather than starting another.
        assert _open(client, cell, port).json() == r.json()
        assert len(service._envs["env-1"].forwarders) == 1
        # The environment's close takes its host port out of the run's.
        service.close("env-1")
        assert host_port not in run.bound_ports
    finally:
        app.close()


def test_concurrent_opens_of_one_container_port_share_one_forwarder(client, cell, live):
    """Two opens of the same container port at once (two viewers, a double
    submit) get the same host port from one forwarder."""
    import threading
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment
    run = live.start(cell, listen=False)
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", _Container())
    barrier = threading.Barrier(4)
    got: list[int] = []

    def expose():
        barrier.wait()
        got.append(service.expose_port("env-1", 5173, serve=True))

    threads = [threading.Thread(target=expose) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert len(set(got)) == 1 and len(got) == 4
        assert len(service._envs["env-1"].forwarders) == 1
        assert got[0] in run.bound_ports
    finally:
        service.close("env-1")
    assert got[0] not in run.bound_ports


@pytest.mark.parametrize("bridge", [True, False], ids=["bridge", "relay"])
def test_an_environment_closing_before_the_forward_serves_nothing(client, cell, live, bridge):
    """The environment closes between the probe that found its container
    listening and the forward: no forwarder, no bound port, a 404."""
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment, PortListener
    run = live.start(cell, listen=False)
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", _Container())
    run._environment_service = service

    def closing_probe(port, **_):
        service.close("env-1")
        return PortListener("env-1", port, bridge=bridge, address="127.0.0.1", relay="python3")

    service.find_listener = closing_probe
    before = set(run.bound_ports)
    r = _open(client, cell, 5173)
    assert r.status_code == 404, r.text
    assert service._envs["env-1"].forwarders == []
    assert set(run.bound_ports) == before


class _LocalExecAPI:
    """docker-py's low-level exec calls, run on this machine with ``PATH``
    as the image's: a plain exec answers its output as one chunk, an
    attached one (``socket=True``) answers a socket that carries the
    process's stdin raw and its stdout and stderr in docker's 8-byte-header
    frames, its EOF the process's end."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.execs: dict[str, dict] = {}
        self.running: set = set()

    def exec_create(self, container, cmd, stdin=False, tty=False, workdir=None, user="", environment=None, **_):
        exec_id = uuid.uuid4().hex
        self.execs[exec_id] = {"cmd": list(cmd), "stdin": stdin, "tty": tty, "workdir": workdir,
                               "user": user, "code": None}
        return {"Id": exec_id}

    def exec_start(self, exec_id, stream=False, socket=False, **_):
        record = self.execs[exec_id]
        env = {"PATH": self.path}
        if not socket:
            done = subprocess.run(record["cmd"], env=env, cwd=record["workdir"], capture_output=True)
            record["code"] = done.returncode
            return iter([done.stdout + done.stderr])
        import socket as sockets
        import threading
        proc = subprocess.Popen(record["cmd"], env=env, cwd=record["workdir"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.running.add(proc)
        ours, theirs = sockets.socketpair()
        lock = threading.Lock()

        def stdin():
            try:
                while data := theirs.recv(65536):
                    proc.stdin.write(data)
                    proc.stdin.flush()
                proc.stdin.close()
            except OSError:  # the process or the host's end gone
                pass

        def output(pipe, stream):
            while data := os.read(pipe.fileno(), 65536):
                try:
                    with lock:
                        theirs.sendall(bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data)
                except OSError:  # the host closed its end: the output goes nowhere
                    pass

        def run():
            pumps = [threading.Thread(target=output, args=(proc.stdout, 1), daemon=True),
                     threading.Thread(target=output, args=(proc.stderr, 2), daemon=True)]
            for pump in pumps:
                pump.start()
            for pump in pumps:
                pump.join()
            record["code"] = proc.wait()
            self.running.discard(proc)
            theirs.close()

        threading.Thread(target=stdin, daemon=True).start()
        threading.Thread(target=run, daemon=True).start()
        return ours

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.execs[exec_id]["code"]}


class _LocalContainer(_Container):
    """A placed container whose bridge address (127.0.0.2) a server bound
    to 127.0.0.1 refuses, and whose execs run here (``_LocalExecAPI``)."""
    attrs = {"NetworkSettings": {"IPAddress": "127.0.0.2"}}

    def __init__(self, path: str) -> None:
        self.client = types.SimpleNamespace(api=_LocalExecAPI(path))


def _image_path(tmp_path, *programs: str) -> str:
    """A ``PATH`` holding only sh, cat and ``programs`` from this machine."""
    import shutil
    folder = tmp_path / f"image-bin-{uuid.uuid4().hex[:6]}"
    folder.mkdir()
    for name in ("sh", "cat", *programs):
        found = shutil.which(name)
        if found is None:
            pytest.skip(f"no {name} on this machine")
        (folder / name).symlink_to(found)
    return str(folder)


@pytest.fixture
def loopback_server():
    """A server bound to 127.0.0.1 that reads its client to EOF and answers
    ``got:<what it read>``, so a relay must carry a half-close both ways."""
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                got = b""
                while data := conn.recv(65536):
                    got += data
                conn.sendall(b"got:" + got)

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


def _half_closed_exchange(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
        c.sendall(payload)
        c.shutdown(socket.SHUT_WR)
        answer = b""
        while data := c.recv(65536):
            answer += data
        return answer


@pytest.mark.parametrize("program", ["python3", "node", "socat"])
def test_a_port_a_container_listens_on_only_inside_it_is_relayed(client, cell, live, tmp_path,
                                                                 loopback_server, program):
    """The bridge refuses a server bound to the container's own loopback;
    its TCP tables show it listening there, and each connection to the
    host port runs the image's relay program in the container."""
    import time
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment, _relay_argv
    run = live.start(cell, listen=False)
    container = _LocalContainer(_image_path(tmp_path, program))
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", container)
    run._environment_service = service
    port = loopback_server
    r = _open(client, cell, port)
    assert r.status_code == 200, r.text
    placed = service._envs["env-1"]
    host_port = placed.relayed[port]
    assert port not in placed.forwarded
    assert r.json()["url"] == f"/proxy/{CHANNEL}/{run.run_id}/{host_port}/"
    assert json.loads(_files(cell)[r.json()["path"]])["directUrl"] == f"http://127.0.0.1:{host_port}/"
    assert host_port in run.bound_ports and host_port in placed.host_ports
    assert placed.relay_program == program

    assert _half_closed_exchange(host_port, b"ping") == b"got:ping"
    assert _half_closed_exchange(host_port, b"x" * 200_000) == b"got:" + b"x" * 200_000
    relays = [record for record in container.client.api.execs.values() if record["stdin"]]
    assert len(relays) == 2
    assert all(record["cmd"] == _relay_argv(program, "127.0.0.1", port)
               and not record["tty"] and record["user"] == "" for record in relays)

    # A second open reuses the relay; the relay program is looked up once.
    lookups = len(container.client.api.execs)
    assert _open(client, cell, port).json() == r.json()
    assert len(placed.forwarders) == 1
    assert len(container.client.api.execs) == lookups + 1  # the TCP tables only

    # A connection still open when the environment closes is ended with it.
    held = socket.create_connection(("127.0.0.1", host_port), timeout=10)
    deadline = time.monotonic() + 10
    while not container.client.api.running and time.monotonic() < deadline:
        time.sleep(0.02)
    service.close("env-1")
    assert host_port not in run.bound_ports and not placed.relayed
    held.settimeout(10)
    assert held.recv(1) == b""
    held.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", host_port), timeout=2)


def test_a_relay_serves_sockets_numbered_above_selects_ceiling(client, cell, live, tmp_path,
                                                               loopback_server, fds_above_select_ceiling):
    """As for the forwarder: the relay's listener, its clients and the exec's
    attach socket are numbered past select()'s ceiling in the desktop shell's
    server, and each connection must still reach the container's program and
    carry its answer back, half-close included."""
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment
    run = live.start(cell, listen=False)
    container = _LocalContainer(_image_path(tmp_path, "python3"))
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", container)
    run._environment_service = service
    port = loopback_server
    r = _open(client, cell, port)
    assert r.status_code == 200, r.text
    placed = service._envs["env-1"]
    host_port = placed.relayed[port]
    assert placed.forwarders[0].server.fileno() >= 1024
    try:
        assert _half_closed_exchange(host_port, b"ping") == b"got:ping"
        assert _half_closed_exchange(host_port, b"x" * 200_000) == b"got:" + b"x" * 200_000
    finally:
        service.close("env-1")


def test_a_relay_whose_server_is_gone_closes_the_client_and_keeps_listening(client, cell, live, tmp_path,
                                                                            loopback_server):
    """The listener stops after the relay was set up: that connection's
    exec fails and its client is closed, and the relay serves the server
    once it is back on its port."""
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment
    run = live.start(cell, listen=False)
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", _LocalContainer(_image_path(tmp_path, "python3")))
    gone = socket.socket()
    gone.bind(("127.0.0.1", 0))
    gone.listen()
    port = gone.getsockname()[1]
    try:
        listener = service.find_listener(port)
        assert (listener.bridge, listener.address, listener.relay) == (False, "127.0.0.1", "python3")
        host_port = service.expose_listener(listener, serve=True)
        gone.close()
        assert _half_closed_exchange(host_port, b"ping") == b""
        back = socket.socket()
        back.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        back.bind(("127.0.0.1", port))
        back.listen()
        try:
            with socket.create_connection(("127.0.0.1", host_port), timeout=10):
                back.settimeout(10)
                accepted, _ = back.accept()
                accepted.close()
        finally:
            back.close()
    finally:
        gone.close()
        service.close("env-1")
    assert not run.bound_ports


def test_a_loopback_listener_with_no_relay_program_names_why_in_the_404(client, cell, live, tmp_path,
                                                                        loopback_server):
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment
    run = live.start(cell, listen=False)
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", _LocalContainer(_image_path(tmp_path)))
    run._environment_service = service
    port = loopback_server
    r = _open(client, cell, port)
    assert r.status_code == 404
    assert r.json()["detail"] == (
        f"A program in this cell's container listens on 127.0.0.1:{port}, which only that "
        "container reaches. The host relays such a port through python3, node or socat inside "
        "the container, and its image has none of them. Have the program listen on 0.0.0.0 "
        "(for example --host 0.0.0.0) or add python3 to the image.")
    placed = service._envs["env-1"]
    assert not run.bound_ports and not placed.forwarders and not placed.host_ports
    assert not any(p.endswith(".website") for p in _files(cell))
    assert live.reveals == []


def test_a_port_nothing_in_the_container_listens_on_is_the_plain_404(client, cell, live, tmp_path):
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment
    run = live.start(cell, listen=False)
    service = EnvironmentService(run, client_factory=lambda: None)
    service._envs["env-1"] = PlacedEnvironment("env-1", _LocalContainer(_image_path(tmp_path, "python3")))
    run._environment_service = service
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    r = _open(client, cell, port)
    assert r.status_code == 404
    assert r.json()["detail"] == f"No running runner on this cell serves port {port}"
    assert not run.bound_ports and not service._envs["env-1"].forwarders


@pytest.mark.parametrize(("tables", "address"), [
    # 127.0.0.1 and ::1 (as the kernel prints them on little-endian), the
    # loopback first; then a wildcard; an IPv4-mapped IPv6 address reads as IPv4.
    ("0: 00000000:1389 00000000:0000 0A\n1: 0100007F:1389 00000000:0000 0A\n", "127.0.0.1"),
    ("0: 00000000000000000000000001000000:1389 00000000000000000000000000000000:0000 0A\n", "::1"),
    ("0: 0000000000000000FFFF00000100007F:1389 00000000000000000000000000000000:0000 0A\n", "127.0.0.1"),
    ("0: 0100007F:1389 0100007F:9C40 01\n1: 00000000:1389 00000000:0000 0A\n", "0.0.0.0"),
    ("0: 0100007F:138A 00000000:0000 0A\n1: 0100007F:1389 0100007F:9C40 01\n", None),
    ("  sl  local_address rem_address   st\n", None),
])
def test_the_container_tcp_tables_give_the_listening_address(tables, address):
    import sys
    from collimate_server.process_manager.environments import _proc_listen_address
    if sys.byteorder != "little":
        pytest.skip("the rows are written as a little-endian kernel prints them")
    assert _proc_listen_address(tables, 5001) == address


def test_a_wildcard_listener_is_relayed_to_its_familys_loopback():
    from collimate_server.process_manager.environments import _relay_argv, _relay_target
    assert _relay_target("0.0.0.0") == "127.0.0.1"
    assert _relay_target("::") == "::1"
    assert _relay_target("172.17.0.2") == "172.17.0.2"
    assert _relay_argv("socat", "::1", 5001) == ["socat", "STDIO", "TCP6:[::1]:5001"]
    assert _relay_argv("socat", "127.0.0.1", 5001) == ["socat", "STDIO", "TCP4:127.0.0.1:5001"]


@pytest.mark.parametrize("program", ["python3", "node", "socat"])
@pytest.mark.parametrize("size", [70_000, 200_000])
def test_a_relay_whose_output_waits_for_its_reader_loses_nothing(loopback_server, program, size):
    """The relay's stdout reader (docker's attach, the host's relay loop)
    reads only after the server has answered and ended, as a loaded host
    does: the pipe fills and the rest of the answer waits in the relay, and
    the server's end must not exit before it is written (node's stdout on a
    pipe is asynchronous, and process.exit() dropped its queued tail)."""
    import shutil
    import time
    from collimate_server.process_manager.environments import _relay_argv
    if shutil.which(program) is None:
        pytest.skip(f"no {program} on this machine")
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    relay = subprocess.Popen(_relay_argv(program, "127.0.0.1", loopback_server),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        relay.stdin.write(payload)
        relay.stdin.close()
        time.sleep(0.3)
        assert relay.stdout.read() == b"got:" + payload
        assert relay.wait(timeout=10) == 0
    finally:
        relay.kill()
        relay.wait()


def test_nothing_serving_the_port_is_a_404(client, cell, live):
    run = live.start(cell, listen=False)
    nobody = socket.socket()
    nobody.bind(("127.0.0.1", 0))
    nobody.listen()
    try:
        port = nobody.getsockname()[1]
        r = _open(client, cell, port)
        assert r.status_code == 404
        assert r.json()["detail"] == f"No running runner on this cell serves port {port}"
        assert not run.bound_ports
        assert not any(p.endswith(".website") for p in _files(cell))
        assert live.reveals == []
    finally:
        nobody.close()


def test_a_run_on_another_channel_or_ending_serves_nothing_here(client, cell, live):
    run = live.start(cell)
    run.channel = OTHER_CHANNEL
    assert _open(client, cell, run.served_port).status_code == 404
    run.channel = CHANNEL
    run.sealed = True
    assert _open(client, cell, run.served_port).status_code == 404
    run.sealed = False
    run.process.returncode = 0
    assert _open(client, cell, run.served_port).status_code == 404


@pytest.mark.parametrize("body", [
    {"port": 0}, {"port": 65536}, {"port": "5173"}, {"port": True}, {"port": 5173.0},
    {}, {"port": 5173, "run_id": "r"},
])
def test_a_malformed_port_is_a_422(client, cell, body):
    r = client.post(f"/api/channels/{CHANNEL}/cells/{cell}/sites", json=body)
    assert r.status_code == 422, r.text


def test_the_pointer_goes_with_the_run_end_sweep(client, cell, live):
    from collimate_server.cell_commits import sweep_run_folder
    run = live.start(cell)
    path = _open(client, cell, run.served_port).json()["path"]
    assert path in _files(cell)
    assert sweep_run_folder(CHANNEL, cell, run.run_id, run.label)
    assert path not in _files(cell)


def test_the_pointer_is_host_owned_and_outside_the_runs_live_files(client, cell, live):
    """The run may not write or delete the pointer, and its live-files
    mirror never sees it, so it is neither pulled into the workspace nor
    reported as a refused write."""
    run = live.start(cell)
    path = _open(client, cell, run.served_port).json()["path"]
    assert run._run_path_refusal(path, ui_write=True) == {
        "path": path, "reason": "host_owned", "owner": run.label}
    assert run._run_path_refusal(path) == {"path": path, "reason": "host_owned", "owner": run.label}
    assert run._run_path_refusal(f"_runs/{run.label}/preview.website", ui_write=True) is None
    assert path in _files(cell)
    assert path not in run._cell_live_metadata()
    assert path not in run._compute_cell_live_hashes()
