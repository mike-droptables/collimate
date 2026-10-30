"""Host-placed environments (process_manager/environments.py).

The docker half of the env_* IPC ops, against a fake docker client: the
placement spec becomes a container run with the run-id label (the
reaper's backstop) and the instance label (which server owns it, so a
second live server's boot sweep spares it), image ensure builds
base-before-main (moved here with the logic from the SDK's _env_shared),
exec runs argv verbatim, and teardown is idempotent and run-bound.
"""
from __future__ import annotations

import json
from pathlib import Path
import types

import pytest


class _FakeImages:
    def __init__(self, present=()):
        import docker.errors

        self._present = set(present)
        self._not_found = docker.errors.ImageNotFound
        self.built: list = []
        self.build_kwargs: list = []
        self.pulled: list = []

    def get(self, tag):
        if tag in self._present:
            return object()
        raise self._not_found(f"no such image: {tag}")

    def build(self, *, path, tag, rm, buildargs=None, pull=False):
        # buildargs carries ARG BASE (the resolved content-addressed base tag);
        # pull mirrors the run's update_packages grant. Recorded so tests can
        # assert both reach docker.
        self.built.append((tag, path))
        self.build_kwargs.append({"tag": tag, "buildargs": dict(buildargs or {}), "pull": pull})
        self._present.add(tag)
        return (object(), iter([{"stream": f"Step 1/1 : (built {tag})"}]))

    def pull(self, repo, tag=None):
        ref = f"{repo}:{tag}" if tag else repo
        self.pulled.append(ref)
        self._present.add(ref)
        return object()


class _FakeExecStream:
    """docker-py's ``CancellableStream``: an iterator the caller closes."""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed = True


class _FakeExecAPI:
    """docker-py's low-level exec calls (``client.api``) for one container:
    each exec records its call like ``exec_run`` does and streams the
    container's ``exec_chunks``, then reports ``exec_exit_code``."""

    def __init__(self, container):
        self._container = container
        self.started: list = []
        self.streams: list = []

    def exec_create(self, container_id, cmd, *, workdir=None, environment=None, user=""):
        assert container_id == self._container.id
        self._container.exec_calls.append(
            {"cmd": cmd, "workdir": workdir, "environment": environment,
             "user": user, "detach": False}
        )
        return {"Id": f"exec-{len(self._container.exec_calls)}"}

    def exec_start(self, exec_id, *, stream=False):
        self.started.append({"exec_id": exec_id, "stream": stream})
        self.streams.append(_FakeExecStream(self._container.exec_chunks))
        return self.streams[-1]

    def exec_inspect(self, exec_id):
        return {"ExitCode": self._container.exec_exit_code}


class _FakeContainer:
    def __init__(self, name, kwargs):
        self.id = f"cid-{name}"
        self.name = name
        self.run_kwargs = kwargs
        self.exec_calls: list = []
        self.exec_chunks = [b"OK"]
        self.exec_exit_code = 0
        self.client = types.SimpleNamespace(api=_FakeExecAPI(self))
        self.stopped = False
        self.removed = False
        # What docker reports once the container runs: each ("ip", port)
        # the spec asked for, published.
        published = {
            key: [{"HostIp": ip, "HostPort": str(port)}]
            for key, (ip, port) in (kwargs.get("ports") or {}).items()
        }
        self.attrs = {"NetworkSettings": {"Ports": published}}

    def reload(self):
        return None

    def exec_run(self, cmd, *, workdir=None, environment=None, demux=False,
                 user="", detach=False):
        # Only a detached exec goes through exec_run; a waited one streams
        # through client.api.
        assert detach
        self.exec_calls.append(
            {"cmd": cmd, "workdir": workdir, "environment": environment,
             "user": user, "detach": detach}
        )
        return types.SimpleNamespace(exit_code=None, output=None)

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class _FakeContainers:
    def __init__(self):
        self.ran: list[_FakeContainer] = []

    def run(self, image, *, detach, name, volumes, environment, **kwargs):
        container = _FakeContainer(
            name,
            {"image": image, "volumes": volumes, "environment": environment, **kwargs},
        )
        self.ran.append(container)
        return container


class _FakeClient:
    def __init__(self, present=()):
        self.images = _FakeImages(present)
        self.containers = _FakeContainers()


class _StubManager:
    run_id = "run-env-test"

    def __init__(self, workspace_dir):
        from runner_permissions import RunnerPermissions
        self.workspace_dir = workspace_dir
        # The environment plane needs docker: full.
        self.permissions = RunnerPermissions.from_definition({"docker": "full"})
        self.log_buffer: list = []
        self.bound_ports: set = set()


class _Writer:
    """A run's IPC writer: every frame the host sends back."""

    def __init__(self):
        self.frames: list[dict] = []

    def write(self, data: bytes) -> None:
        self.frames.append(json.loads(data))

    async def drain(self) -> None:
        return None


def run_with_environment(workspace, **spec):
    """A run (the real IPC mixin) that has placed one environment on a fake
    docker client. Returns ``(run, container)``."""
    from process_manager.environments import EnvironmentService
    from process_manager.ipc import IPCMixin
    from runner_permissions import RunnerPermissions

    class _Run(IPCMixin):
        pass

    run = _Run()
    run.run_id = "run-own"
    # The environment plane (env_place, terminals) needs docker: full.
    run.permissions = RunnerPermissions.from_definition({"docker": "full"})
    run.workspace_dir = str(workspace)
    run.log_buffer = []
    run.bound_ports = set()
    run.cell_id = "cell-own"
    run.backend = None
    run._ipc_writer = _Writer()
    client = _FakeClient(present=("present:latest",))
    run._environment_service = EnvironmentService(run, client_factory=lambda: client)
    run.environment_service.place({"image": "present:latest", **spec})
    return run, client.containers.ran[0]


def refusals(run) -> list[str]:
    """The refusals the host logged in the run's log."""
    return [row["payload"] for row in run.log_buffer if row["type"] == "ipc_error"]


@pytest.fixture
def service(tmp_path):
    from process_manager.environments import EnvironmentService

    client = _FakeClient(present=("present:latest",))
    for name in ("tool", "base", "vscode"):
        (tmp_path / name).mkdir()
    svc = EnvironmentService(_StubManager(str(tmp_path)), client_factory=lambda: client)
    return svc, client


def test_place_runs_labelled_container_with_workspace_mount(service, tmp_path):
    svc, client = service
    placed = svc.place({
        "image": "present:latest",
        "mount": "/work",
        "mount_mode": "rw",
        "workspace": str(tmp_path),
        "environment": {"K": "v"},
        "command": "sleep infinity",
        "extra_hosts": {"host.docker.internal": "host-gateway"},
    })
    assert placed["env_id"] == "env-1"
    container = client.containers.ran[0]
    assert placed["container_id"] == container.id
    kw = container.run_kwargs
    assert kw["volumes"][str(tmp_path)] == {"bind": "/work", "mode": "rw"}
    assert kw["labels"]["com.collimate.run_id"] == "run-env-test"
    # The owner stamp: the startup reaper of a SECOND server on this dockerd
    # (a dev run beside the installed app) keeps containers whose instance
    # label isn't its own, so placement must carry ours.
    from instance import INSTANCE_LABEL, instance_id
    assert kw["labels"][INSTANCE_LABEL] == instance_id()
    assert kw["command"] == "sleep infinity"
    assert kw["working_dir"] == "/work"
    assert kw["extra_hosts"] == {"host.docker.internal": "host-gateway"}


def test_place_command_null_uses_image_entrypoint(service, tmp_path):
    svc, client = service
    svc.place({"image": "present:latest", "workspace": str(tmp_path), "command": None})
    assert "command" not in client.containers.ran[0].run_kwargs


def test_a_port_bound_to_one_interface_reaches_docker_as_a_pair(service, tmp_path):
    """A generator binds a port as ``(address, port)``; the spec crosses the
    IPC as JSON, so the host receives a list. docker-py reads a list as
    several host ports and Docker refuses ``"127.0.0.1"`` as a port, which
    kept an app-mode run from starting. The host hands docker the pair."""
    svc, client = service
    spec = json.loads(json.dumps({
        "image": "present:latest",
        "workspace": str(tmp_path),
        "ports": {"8080/tcp": ("127.0.0.1", 18999)},
    }))
    assert spec["ports"]["8080/tcp"] == ["127.0.0.1", 18999]
    svc.place(spec)
    sent = client.containers.ran[-1].run_kwargs["ports"]
    assert sent == {"8080/tcp": ("127.0.0.1", 18999)}


def test_placed_containers_carry_a_pids_limit(service, tmp_path):
    # Harness and sandbox containers get the same fork-bomb backstop as a
    # docker:limited run, not just whatever TasksMax the host happens to set.
    from process_manager.docker_runner import _DEFAULT_PIDS_LIMIT
    svc, client = service
    svc.place({"image": "present:latest", "workspace": str(tmp_path)})
    assert client.containers.ran[0].run_kwargs["pids_limit"] == _DEFAULT_PIDS_LIMIT


def test_ensure_image_builds_base_before_main(service):
    svc, client = service
    svc.place({
        "image": "collimate-tool:latest",
        "build_context": str(Path(svc._manager.workspace_dir) / "tool"),
        "base_image": "collimate-base:v9",
        "base_context": str(Path(svc._manager.workspace_dir) / "base"),
    })
    assert [t for t, _ in client.images.built] == [
        "collimate-base:v9", "collimate-tool:latest",
    ]


def test_ensure_image_skips_present_base(service):
    svc, client = service
    svc.place({
        "image": "collimate-tool:latest",
        "build_context": str(Path(svc._manager.workspace_dir) / "tool"),
        "base_image": "present:latest",
        "base_context": str(Path(svc._manager.workspace_dir) / "base"),
    })
    assert [t for t, _ in client.images.built] == ["collimate-tool:latest"]


def test_ensure_image_missing_base_no_context_raises(service):
    """A ``collimate-*`` image is one WE build, so no build context is a wiring
    bug — it must say so rather than attempt a pull that would fail against a
    registry with a misleading error."""
    svc, client = service
    with pytest.raises(RuntimeError, match="must be built, not pulled"):
        svc.place({
            "image": "collimate-tool:latest",
            "build_context": str(Path(svc._manager.workspace_dir) / "tool"),
            "base_image": "collimate-base:v9",
        })
    assert client.images.built == []
    assert client.images.pulled == []
    assert client.containers.ran == []


def test_registry_image_without_context_is_pulled(service):
    """The counterpart, and a real bug fixed: a relay may put an arbitrary
    registry tag in its ``image`` setting (code-runner / linux-env expose that
    field). There is no build context for one, and before this the host raised
    instead of pulling it."""
    svc, client = service
    svc.place({"image": "python:3.12-slim"})
    assert client.images.pulled == ["python:3.12-slim"]
    assert client.images.built == []


def test_registry_digest_image_is_pulled_whole(service):
    """A digest-pinned reference has no tag to split off: splitting on its
    last colon would pull ``img:<hex>`` from repository ``img@sha256``."""
    svc, client = service
    ref = "python@sha256:" + "a" * 64
    svc.place({"image": ref})
    assert client.images.pulled == [ref]


def test_split_ref_keeps_registry_ports_and_digests():
    from process_manager.environments import _split_ref
    assert _split_ref("python:3.12-slim") == ("python", "3.12-slim")
    assert _split_ref("reg.local:5000/img") == ("reg.local:5000/img", "latest")
    assert _split_ref("img@sha256:x") == ("img@sha256:x", None)


def test_registry_pull_is_refused_without_the_update_grant(service):
    """Pulling reaches upstream, so it rides ``update_packages``."""
    svc, client = service
    with pytest.raises(RuntimeError, match="may not fetch it"):
        svc.place({"image": "python:3.12-slim", "refresh": False})
    assert client.images.pulled == []


def test_a_placement_that_does_not_say_follows_the_run_update_grant(service):
    """With no ``refresh`` in the spec, the run's ``update_packages`` decides
    whether the host may fetch upstream."""
    from runner_permissions import RunnerPermissions
    svc, client = service
    svc._manager.permissions = RunnerPermissions.from_definition(
        {"docker": "full", "update_packages": False})
    with pytest.raises(RuntimeError, match="may not fetch it"):
        svc.place({"image": "python:3.12-slim"})
    assert client.images.pulled == []
    svc._manager.permissions = RunnerPermissions.from_definition({"docker": "full"})
    svc.place({"image": "python:3.12-slim"})
    assert client.images.pulled == ["python:3.12-slim"]


def test_build_receives_base_arg_and_pull_flag(service):
    """``ARG BASE`` reaches docker as a build arg, and the run's refresh
    decision reaches ``pull`` — but ONLY for the build whose ``FROM`` is
    upstream. ``--pull`` re-pulls every ``FROM``, so handing it to an image
    layered on our locally-built base fails the build outright ("pull access
    denied for collimate-linux-small"); the upstream fetch belongs to the
    base's own build, whose FROM is debian."""
    svc, client = service
    svc.place({
        "image": "collimate-tool:abc123",
        "build_context": str(Path(svc._manager.workspace_dir) / "tool"),
        "base_image": "collimate-linux-small:def456",
        "base_context": str(Path(svc._manager.workspace_dir) / "base"),
        "build_args": {"BASE": "collimate-linux-small:def456"},
        "refresh": True,
    })
    leaf = next(k for k in client.images.build_kwargs if k["tag"] == "collimate-tool:abc123")
    assert leaf["buildargs"] == {"BASE": "collimate-linux-small:def456"}
    assert leaf["pull"] is False, "a local base must never be pulled"
    base = next(k for k in client.images.build_kwargs
                if k["tag"] == "collimate-linux-small:def456")
    assert base["pull"] is True, "the base's own FROM is upstream"

    # A leaf with NO local base FROMs something upstream, so it does pull.
    svc.place({
        "image": "collimate-vscode:aaa111",
        "build_context": str(Path(svc._manager.workspace_dir) / "vscode"),
        "refresh": True,
    })
    solo = next(k for k in client.images.build_kwargs if k["tag"] == "collimate-vscode:aaa111")
    assert solo["pull"] is True
    svc.place({
        "image": "collimate-tool:zzz999",
        "build_context": str(Path(svc._manager.workspace_dir) / "tool"),
        "refresh": False,
    })
    kw2 = next(k for k in client.images.build_kwargs if k["tag"] == "collimate-tool:zzz999")
    assert kw2["pull"] is False


def test_exec_runs_argv_verbatim(service, tmp_path):
    svc, client = service
    placed = svc.place({"image": "present:latest", "workspace": str(tmp_path)})

    rc, out = svc.exec(placed["env_id"], ["timeout", "7", "sh", "-c", "echo hi"],
                       cwd="/work", user="1000:1000")
    assert (rc, out) == (0, "OK")
    call = client.containers.ran[0].exec_calls[0]
    assert call["cmd"] == ["timeout", "7", "sh", "-c", "echo hi"]
    assert call["workdir"] == "/work" and call["user"] == "1000:1000"

    rc, out = svc.exec(placed["env_id"], ["sh", "-c", "launcher"], detach=True)
    assert (rc, out) == (0, "")
    assert client.containers.ran[0].exec_calls[1]["detach"] is True


def test_exec_output_is_capped_to_its_tail_on_the_host__F1669(service, tmp_path, monkeypatch):
    """A command that prints without end is streamed through a bounded
    tail: the host holds (and later json-serializes into the ack) only the
    last bytes, marked with how many were dropped, and still reports the
    exit code. Marker and tail together fit the cap, so the SDK's routed
    reply cap (the same size, keeping the head) never cuts the tail's end;
    the stream is closed once read."""
    import process_manager.environments as environments

    monkeypatch.setattr(environments, "_EXEC_OUTPUT_CAP", 1000)
    svc, client = service
    placed = svc.place({"image": "present:latest", "workspace": str(tmp_path)})
    container = client.containers.ran[0]
    container.exec_chunks = ((b"%04d" % i) * 25 for i in range(500))  # 50 KB
    container.exec_exit_code = 3

    rc, out = svc.exec(placed["env_id"], ["sh", "-c", "yes"])
    assert rc == 3
    assert len(out) <= 1000
    marker, body = out.split("\n", 1)
    kept = 1000 - environments._EXEC_MARKER_ROOM
    assert marker == f"[... {50_000 - kept} earlier bytes of output dropped]"
    assert len(body) == kept and body.endswith("0499" * 25)
    api = container.client.api
    assert api.started == [{"exec_id": "exec-1", "stream": True}]
    assert api.streams[-1].closed

    # Output under the cap comes back whole, unmarked; a missing exit code
    # (the daemon lost the exec) is -1.
    container.exec_chunks = [b"short ", b"output"]
    container.exec_exit_code = None
    assert svc.exec(placed["env_id"], ["true"]) == (-1, "short output")
    assert api.streams[-1].closed

    # One past the cap is cut too, still within it.
    container.exec_chunks = [b"y" * 1001]
    _rc, out = svc.exec(placed["env_id"], ["true"])
    assert len(out) <= 1000 and out.startswith("[... ")


def test_exec_output_tail_holds_a_bounded_buffer__F1669():
    """The tail is a ring, not a join-then-slice: however much a command
    prints, the host holds a small multiple of the cap."""
    import tracemalloc

    from process_manager.environments import _output_tail

    cap = 20_000
    chunk = b"z" * 4096

    def chunks():
        for _ in range(2_000):  # ~8 MB, 400x the cap
            yield chunk

    tracemalloc.start()
    try:
        out = _output_tail(chunks(), cap)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(out) <= cap
    assert peak < 6 * cap, peak


def test_exec_stream_that_cannot_close_still_returns_its_output__F1669(service, tmp_path):
    """A stream whose socket is already gone (or an ssh:// daemon, whose
    streams docker-py cannot cancel) does not fail an exec that ran."""
    svc, client = service
    placed = svc.place({"image": "present:latest", "workspace": str(tmp_path)})
    api = client.containers.ran[0].client.api
    start = api.exec_start

    def exec_start(exec_id, *, stream=False):
        s = start(exec_id, stream=stream)

        def close():
            raise OSError("socket already closed")

        s.close = close
        return s

    api.exec_start = exec_start
    assert svc.exec(placed["env_id"], ["true"]) == (0, "OK")


def test_exec_unknown_environment_raises(service):
    svc, _ = service
    with pytest.raises(RuntimeError, match="no such environment"):
        svc.exec("env-404", ["true"])


def test_close_is_idempotent_and_close_all_reaps(service, tmp_path):
    svc, client = service
    a = svc.place({"image": "present:latest", "workspace": str(tmp_path)})
    b = svc.place({"image": "present:latest", "workspace": str(tmp_path)})

    svc.close(a["env_id"])
    svc.close(a["env_id"])
    first = client.containers.ran[0]
    assert first.stopped and first.removed

    svc.close_all()
    second = client.containers.ran[1]
    assert second.stopped and second.removed
    # A closed environment refuses further exec.
    with pytest.raises(RuntimeError, match="no such environment"):
        svc.exec(b["env_id"], ["true"])


@pytest.fixture
def echo_server():
    """A loopback echo listener: ``(host, port)``."""
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.2)
    stop = threading.Event()

    def _echo(conn):
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            conn.settimeout(None)
            threading.Thread(target=_echo, args=(conn,), daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()
    yield srv.getsockname()
    stop.set()
    srv.close()


def _roundtrip(port: int, payload: bytes) -> bytes:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
        c.sendall(payload)
        return c.recv(65536)


def test_forwarders_get_distinct_kernel_assigned_ports(echo_server):
    """Each forwarder binds its own port (no probe-then-rebind race), so two
    concurrent placements cannot be handed the same one."""
    from process_manager.environments import PlacedEnvironment, _start_forwarder

    host, port = echo_server
    placed = PlacedEnvironment("env-1", container=None)
    try:
        a = _start_forwarder(placed, host, port)
        b = _start_forwarder(placed, host, port)
        assert a and b and a != b
        assert _roundtrip(a, b"one") == b"one"
        assert _roundtrip(b, b"two") == b"two"
    finally:
        for srv, stop in placed.forwarders:
            stop.set()
            srv.close()


def test_a_stalled_upstream_connect_does_not_block_other_clients(echo_server, monkeypatch):
    """The upstream connect runs off the accept thread: while one connection
    waits on a target that does not answer, the next is still served."""
    import socket
    import threading

    from process_manager import environments

    real = socket.create_connection
    release = threading.Event()
    calls = []

    def _connect(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(environments.socket, "create_connection", _connect)
    host, port = echo_server
    placed = environments.PlacedEnvironment("env-1", container=None)
    stalled = None
    try:
        fwd = environments._start_forwarder(placed, host, port)
        stalled = real(("127.0.0.1", fwd), timeout=2)
        # Wait until the first connection's upstream connect is in flight.
        for _ in range(100):
            if calls:
                break
            threading.Event().wait(0.02)
        assert calls
        with real(("127.0.0.1", fwd), timeout=2) as c:
            c.sendall(b"ping")
            assert c.recv(65536) == b"ping"
    finally:
        release.set()
        if stalled is not None:
            stalled.close()
        for srv, stop in placed.forwarders:
            stop.set()
            srv.close()


# -- workspace permissions at placement ------------------------------------------


def _mode(path) -> int:
    import os
    import stat
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_placement_never_chmods_through_a_planted_symlink__F0156(service, tmp_path):
    """Container code can write the mounted workspace, so a symlink it plants
    must not make the next mounted placement change a host file (or a host
    directory's tree) outside the workspace."""
    import os

    svc, _client = service
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "id_ed25519"
    secret.write_text("KEY")
    secret.chmod(0o600)
    inner_dir = outside / "private"
    inner_dir.mkdir()
    inner_dir.chmod(0o700)
    inner_file = inner_dir / "token"
    inner_file.write_text("T")
    inner_file.chmod(0o600)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "key").symlink_to(secret)
    (ws / "dirlink").symlink_to(inner_dir, target_is_directory=True)

    svc.place({"image": "present:latest", "workspace": str(ws), "mount": "/work"})

    assert _mode(secret) == 0o600
    assert _mode(inner_dir) == 0o700
    assert _mode(inner_file) == 0o600
    assert os.path.islink(ws / "key") and os.path.islink(ws / "dirlink")


def test_placement_only_adds_missing_bits_and_keeps_exec_bits__F0156(service, tmp_path):
    """A container user who is not the host user needs to traverse every
    directory and read every file; nothing else about a mode changes, so
    scripts and binaries keep their exec bits."""
    svc, _client = service
    ws = tmp_path / "ws"
    ws.mkdir()
    ws.chmod(0o700)
    sub = ws / "bin"
    sub.mkdir()
    sub.chmod(0o700)
    script = sub / "gradlew"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    private_script = sub / "hook"
    private_script.write_text("#!/bin/sh\n")
    private_script.chmod(0o700)
    data = ws / "notes.txt"
    data.write_text("n")
    data.chmod(0o600)
    group_writable = ws / "shared.txt"
    group_writable.write_text("s")
    group_writable.chmod(0o664)

    svc.place({"image": "present:latest", "workspace": str(ws), "mount": "/work"})

    assert _mode(ws) == 0o755
    assert _mode(sub) == 0o755
    assert _mode(script) == 0o755
    assert _mode(private_script) == 0o744
    assert _mode(data) == 0o644
    assert _mode(group_writable) == 0o664


def test_an_entry_swapped_for_a_symlink_after_the_listing_is_refused__F0156(tmp_path, monkeypatch):
    """The check and the change are one open: an entry that becomes a symlink
    between its stat and the change is refused by O_NOFOLLOW, never chmodded
    through."""
    import os

    from process_manager import environments

    outside = tmp_path / "outside"
    outside.write_text("KEY")
    outside.chmod(0o600)
    ws = tmp_path / "ws"
    ws.mkdir()
    victim = ws / "victim"
    victim.write_text("v")
    victim.chmod(0o600)

    real_stat = os.stat

    def _stat_then_swap(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == "victim":
            victim.unlink()
            victim.symlink_to(outside)
        return result

    monkeypatch.setattr(environments.os, "stat", _stat_then_swap)
    environments._relax_perms(str(ws))
    monkeypatch.undo()

    assert os.path.islink(victim)
    assert _mode(outside) == 0o600


def test_placement_walk_holds_a_bounded_number_of_directory_fds__F0156(tmp_path, monkeypatch):
    """os.fwalk keeps one fd open per level it is inside: a directory chain
    planted by an earlier environment must not let the next placement use
    up the server's descriptors. The walk stops entering at the depth bound
    and leaves what lies below as it is."""
    import os

    from process_manager import environments

    monkeypatch.setattr(environments, "_RELAX_MAX_DEPTH", 3)
    ws = tmp_path / "ws"
    deep = ws
    for level in range(6):
        deep = deep / f"d{level}"
    deep.mkdir(parents=True)
    for d in [ws, *ws.rglob("*")]:
        d.chmod(0o700)

    entered: list[str] = []
    real_fwalk = os.fwalk

    def recording_fwalk(*args, **kwargs):
        for entry in real_fwalk(*args, **kwargs):
            entered.append(entry[0])
            yield entry

    monkeypatch.setattr(environments.os, "fwalk", recording_fwalk)
    environments._relax_perms(str(ws))
    monkeypatch.undo()

    walked = [ws, ws / "d0", ws / "d0" / "d1", ws / "d0" / "d1" / "d2"]
    assert entered == [str(d) for d in walked]
    assert all(_mode(d) == 0o755 for d in walked)
    assert _mode(ws / "d0" / "d1" / "d2" / "d3") == 0o700


def test_cached_final_image_needs_no_pruned_build_base__D06(service, tmp_path):
    svc, client = service
    svc.place({
        'image': 'present:latest', 'base_image': 'pruned-base:1',
        'base_context': str(tmp_path / 'base'), 'build_context': str(tmp_path / 'tool'),
    })
    assert not client.images.built
    assert not client.images.pulled
    assert len(client.containers.ran) == 1


@pytest.mark.parametrize('command', ['sleep infinity', None])
def test_all_environment_placements_use_docker_init__F1965(service, tmp_path, command):
    svc, client = service
    svc.place({'image': 'present:latest', 'workspace': str(tmp_path), 'command': command})
    assert client.containers.ran[0].run_kwargs['init'] is True


def test_host_exec_cap_preserves_a_whole_sdk_read_chunk_and_header__P0F27_F1669(service, tmp_path):
    """Actual SDK framing survives the host's streamed-output tail policy.

    Load the canonical shared runtime library from the editable SDK, execute
    its read command on synthetic bytes, and return each frame through the
    host service before the SDK validates/decodes it. No generator fixture,
    Docker daemon, vendored mirror, or copied chunk/header constant.
    """
    import importlib.util
    import subprocess

    import collimate
    from process_manager.environments import _EXEC_OUTPUT_CAP

    library = Path(collimate.__file__).resolve().parents[3] / "content" / "generators" / "_env_shared" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_p0f27_sdk_environment", library)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reader = module.DockerEnvironment.__new__(module.DockerEnvironment)
    data = (bytes(range(256)) * ((reader._READ_CHUNK + 255) // 256))[:reader._READ_CHUNK]
    file = tmp_path / "synthetic read.bin"
    file.write_bytes(data)
    svc, client = service
    placed = svc.place({"image": "present:latest", "workspace": str(tmp_path)})
    container = client.containers.ran[0]
    frames = []

    def execute(argv, **kwargs):
        result = subprocess.run(argv, capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        # The actual base64 utility wraps its lines; those newlines and the
        # SDK's offset/size/token header all count against the host's cap.
        raw = result.stdout
        container.exec_chunks = [raw[i:i + 65536] for i in range(0, len(raw), 65536)]
        container.exec_exit_code = result.returncode
        rc, output = svc.exec(placed["env_id"], argv)
        assert output == raw.decode(), "the host discarded the SDK read's header or body"
        assert len(raw) <= _EXEC_OUTPUT_CAP
        assert container.client.api.streams[-1].closed
        frames.append(output)
        return rc, output

    reader.exec = execute
    assert reader.read_file(str(file)) == data
    assert len(frames) == 2  # maximum full chunk followed by the EOF frame
    assert frames[0].startswith(f"@1 {len(data)} ")
    assert frames[1].startswith(f"@{len(data) + 1} - ")


@pytest.mark.asyncio
async def test_failed_close_ack_keeps_owned_cleanup_and_revokes_ports__P4F47(tmp_path, monkeypatch):
    """A failed removal must stay eligible for host teardown, never appear successful."""
    run, container = run_with_environment(tmp_path)
    service = run.environment_service
    placed = service._envs["env-1"]
    placed.host_ports.add(43123)
    run.bound_ports.add(43123)
    remove = container.remove
    attempts = []

    def refuse_remove(*, force=False):
        attempts.append(force)
        raise RuntimeError("remote removal refused")

    monkeypatch.setattr(container, "remove", refuse_remove)
    await run._op_env_close({"env_id": "env-1", "req_id": "close-1"})
    assert run._ipc_writer.frames[-1] == {
        "op": "ack_env_close", "ok": False,
        "error": "remote removal refused", "req_id": "close-1",
    }
    assert attempts == [True]
    assert 43123 not in run.bound_ports
    assert not service.serves_port(43123)
    assert service._envs["env-1"].container is container
    with pytest.raises(RuntimeError, match="no such environment"):
        service.exec("env-1", ["true"])
    monkeypatch.setattr(container, "remove", remove)
    service.close_all()
    assert container.removed
    assert placed.container is None
    service.close_all()


@pytest.mark.asyncio
async def test_failed_graceful_stop_can_still_ack_successful_removal__P4F47(tmp_path, monkeypatch):
    run, container = run_with_environment(tmp_path)

    def refuse_stop(*, timeout=None):
        raise RuntimeError("graceful stop refused")

    monkeypatch.setattr(container, "stop", refuse_stop)
    await run._op_env_close({"env_id": "env-1", "req_id": "close-1"})
    assert run._ipc_writer.frames[-1] == {
        "op": "ack_env_close", "ok": True, "error": None, "req_id": "close-1",
    }
    assert container.removed
    await run._op_env_close({"env_id": "env-1", "req_id": "close-2"})
    assert run._ipc_writer.frames[-1]["ok"] is True
