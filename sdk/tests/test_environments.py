# SPDX-License-Identifier: MIT
"""Host-free tests for DockerEnvironment placement and process lifecycle."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from collimate.testing import run_generator

from tests._workspace import GENERATORS


_GEN_DIR = str(GENERATORS)


# ---------------------------------------------------------------------------
# Reference generators (file-level)
# ---------------------------------------------------------------------------


def test_run_oneshot_degrades_cleanly_without_host(bridge_hook):
    # Placement refused by the host (the mediation-era version of "docker
    # unreachable") => the unified `run` generator's oneshot mode reports it
    # cleanly (error.md + the reason logged) rather than crashing.
    bridge_hook["setup"] = lambda b: b.stub_env_place(
        lambda _spec: {
            "ok": False, "env_id": "", "container_id": "",
            "error": "docker daemon unreachable",
        }
    )
    res = run_generator(
        os.path.join(_GEN_DIR, "launch", "launch", "run.py"),
        inputs={"main.py": "print('hi')"},
        config={"settings.set_yaml": {"mode": "oneshot", "command": "python main.py"}},
        timeout=20,
    )
    assert res.ok, res.error
    assert "could not start the container environment" in res.files.get("error.md", "")
    # _fail logs the same reason — the only place a run now says why.
    assert any("could not start the container environment" in line for line in res.logs)


def test_run_module_imports():
    # The unified runner imports the Docker-free _env_shared helper at module
    # load (DockerEnvironment only imports docker inside __init__), so importing
    # the entrypoint never needs a Docker daemon.
    import importlib.util
    import sys

    if _GEN_DIR not in sys.path:
        sys.path.insert(0, _GEN_DIR)
    path = os.path.join(_GEN_DIR, "launch", "launch", "run.py")
    spec = importlib.util.spec_from_file_location("_run_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.run)


# ---------------------------------------------------------------------------
# DockerEnvironment — the host-mediated client (bridge env_* ops)
#
# The library no longer talks to a Docker daemon: it declares a placement
# spec and drives the container through the bridge. These tests pin the
# client side of that contract host-free; the host's docker behavior
# (image ensure ordering, container run shape, forwarders) is pinned by
# the app repo's EnvironmentService tests against a fake docker client.
# ---------------------------------------------------------------------------


def _docker_env_module():
    """Import ``_env_shared`` off the generators path (the sibling-cell layout
    the Docker generators use)."""
    import sys

    if _GEN_DIR not in sys.path:
        sys.path.insert(0, _GEN_DIR)
    import _env_shared  # noqa: E402

    return _env_shared


def _make_env(native_bridge, **kwargs):
    DockerEnvironment = _docker_env_module().DockerEnvironment
    return DockerEnvironment(image="collimate-tool:latest", **kwargs)


def test_docker_environment_places_through_the_host(native_bridge, tmp_path):
    env = _make_env(
        native_bridge,
        build_context="build",
        base_image="collimate-base:v9",
        base_context="base",
        environment={"K": "v"},
        working_dir="/srv",
    )
    ops = native_bridge.env_ops
    assert ops[0]["op"] == "env_place"
    spec = ops[0]["spec"]
    assert spec["image"] == "collimate-tool:latest"
    assert spec["mount"] == "/work" and spec["mount_mode"] == "rw"
    # Contexts are resolved to absolute paths before crossing the bridge.
    assert os.path.isabs(spec["build_context"]) and spec["build_context"].endswith("build")
    assert spec["base_image"] == "collimate-base:v9"
    assert spec["environment"] == {"K": "v"}
    assert spec["working_dir"] == "/srv"
    assert env.container_id == "recorded-container"


def test_exec_shaping_stays_client_side(native_bridge):
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, "out"))

    rc, out = env.exec("echo hi | wc -l", timeout=7)
    assert (rc, out) == (0, "out")
    call = [o for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    # String command → sh -c; timeout → coreutils wrapper — exactly the
    # pre-mediation quoting semantics, decided here, run verbatim there.
    assert call["command"][:4] == ["timeout", "7", "sh", "-c"]
    assert call["command"][4] == "echo hi | wc -l"
    assert call["cwd"] == "/work"

    env.exec(["ls", "-la"])
    call = [o for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    assert call["command"] == ["ls", "-la"]


def test_exec_background_launcher_and_wait(native_bridge):
    env = _make_env(native_bridge)
    token = env.exec_background("sleep 5")
    assert token == "colproc-1"
    launch = [o for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    assert launch["detach"] is True
    assert "setsid sh -c" in launch["command"][2]

    # wait_process polls the rcfile via exec until it holds a digit.
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, "3\n"))
    assert env.wait_process(token, timeout=2) == 3


def _run_inner(launch, tmp_path):
    """Recover the wrapper script from a recorded launcher and run it with its
    rc file redirected into ``tmp_path``; return what the rc file holds."""
    import shlex
    import subprocess

    parts = shlex.split(launch["command"][2])
    inner = parts[parts.index("-c") + 1]
    rcfile = tmp_path / "rc"
    inner = inner.replace("/tmp/colproc-", str(tmp_path / "colproc-"))
    inner = inner.rsplit("> ", 1)[0] + f"> {rcfile}"
    subprocess.run(["sh", "-c", inner], check=False)
    return rcfile.read_text().strip()


def test_exec_background_rc_survives_a_trailing_comment_or_exit(native_bridge, tmp_path):
    env = _make_env(native_bridge)
    env.exec_background("exit 3  # trailing comment")
    launch = [o for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    assert _run_inner(launch, tmp_path) == "3"
    env.exec_background("echo hi # c")
    launch = [o for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    assert _run_inner(launch, tmp_path) == "0"


def test_exec_background_raises_when_the_host_cannot_launch(native_bridge):
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: {"ok": False, "error": "environment closed"})
    with pytest.raises(RuntimeError, match="environment closed"):
        env.exec_background("sleep 5")
    assert env._procs == {}


def test_wait_process_gives_up_when_no_exit_code_can_come(native_bridge):
    env = _make_env(native_bridge)
    # The process vanished without writing its rc file.
    token = env.exec_background("sleep 5")
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, "GONE\n"))
    assert env.wait_process(token) == -1
    # The host could not run the probe (env gone).
    native_bridge.stub_env_exec(lambda _id, cmd, kw: {"ok": False, "error": "exec failed"})
    assert env.wait_process(token) == -1
    # stop_process removed it: no rc file is coming.
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, "RUNNING\n"))
    env.stop_process(token)
    assert env.wait_process(token) == -1


def test_wait_process_stops_polling_once_stopped(native_bridge):
    import threading

    env = _make_env(native_bridge)
    token = env.exec_background("sleep 5")
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, "RUNNING\n"))
    result: list = []
    t = threading.Thread(target=lambda: result.append(env.wait_process(token)))
    t.start()
    time.sleep(0.2)
    env.stop_process(token)
    t.join(timeout=5)
    assert result == [-1]


def test_container_shares_one_docker_client_and_inspects_each_time(native_bridge, monkeypatch):
    import sys
    import types

    calls = {"from_env": 0, "get": []}

    class _Containers:
        def get(self, cid):
            calls["get"].append(cid)
            return object()

    def from_env():
        calls["from_env"] += 1
        return types.SimpleNamespace(containers=_Containers())

    monkeypatch.setitem(sys.modules, "docker", types.SimpleNamespace(from_env=from_env))
    mod = _docker_env_module()
    mod._docker_client.cache_clear()
    try:
        env = _make_env(native_bridge)
        env.container
        env.container
    finally:
        mod._docker_client.cache_clear()
    assert calls["from_env"] == 1
    assert calls["get"] == ["recorded-container", "recorded-container"]


def test_expose_port_registers_the_proxy_route(native_bridge):
    # The proxy route names the run's channel and run id, as the bridge has
    # them.
    native_bridge.channel, native_bridge.run_id = "chan", "run-1"
    env = _make_env(native_bridge)

    endpoint = asyncio.run(env.expose_port(8080))
    expose = [o for o in native_bridge.env_ops if o["op"] == "env_expose_port"][-1]
    assert expose["port"] == 8080
    # The host returned the forwarded host port; the endpoint rides the
    # host's proxy route (gen.proxy_url), and the port is registered so the
    # cell's host serves it.
    assert endpoint.port == 40080
    assert endpoint.url == "/proxy/chan/run-1/40080/"
    served = [c for c in native_bridge._rec_tier_b if c["op"] == "serve_port"]
    assert served and served[-1]["port"] == 40080












def test_freeze_stops_every_process_but_pid_1_as_root__F1966(native_bridge, monkeypatch):
    """``kill -STOP -1`` from the shell's own builtin, as root and with no
    ``timeout`` wrapper (a stopped wrapper would never return the exec).
    Exit 1, nothing else to stop, is not a failure; a host that could not
    run it, or a connection that broke under it, is logged and never
    raised: the teardown that called it (its hand-back, then the close)
    goes on."""
    import gen

    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (1, "sh: kill: No such process"))
    env.freeze()
    call = [o for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    assert call["command"] == ["sh", "-c", "kill -STOP -1"]
    assert call["user"] == "0:0" and call["timeout"] is None
    logged: list = []
    monkeypatch.setattr(gen, "log", lambda msg, *a, **k: logged.append(msg))
    env.freeze()
    assert logged == []
    native_bridge.stub_env_exec(lambda _id, cmd, kw: {"ok": False, "error": "gone"})
    env.freeze()
    assert logged and "could not stop the container's processes" in logged[-1]

    def broken(_id, cmd, kw):
        raise RuntimeError("host closed IPC channel while awaiting ack")

    native_bridge.stub_env_exec(broken)
    logged.clear()
    env.freeze()
    assert len(logged) == 1 and "could not stop the container's processes" in logged[0]
    assert "host closed IPC channel" in logged[0]


def _step(op) -> str:
    """One environment op as a teardown step: the SIGTERM to background
    process groups, a liveness probe with the groups it asks about, the
    freeze; any other exec as its argv, anything else as its op."""
    DockerEnvironment = _docker_env_module().DockerEnvironment
    if op["op"] != "env_exec":
        return op["op"]
    cmd = op["command"]
    script = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
    if 'kill -- -"$pid"' in script:
        return "stop"
    if script == DockerEnvironment._LIVE_GROUPS_SCRIPT:
        return "probe " + " ".join(cmd[4:])
    if script == "kill -STOP -1":
        return "freeze"
    return " ".join(cmd)


def _group_4242(alive, clock=None):
    """An exec stub for a background process whose group is 4242: the
    SIGTERM reaches it, and the n-th liveness probe finds it running when
    ``alive[n]`` is true (the last item answers every later probe).
    ``clock`` collects each exec's step and when it ran."""
    DockerEnvironment = _docker_env_module().DockerEnvironment
    answers = list(alive)

    def stub(_id, cmd, kw):
        if clock is not None:
            clock.append((_step({"op": "env_exec", "command": cmd}), time.monotonic()))
        script = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
        if 'kill -- -"$pid"' in script:
            return (0, "sigterm:4242\n")
        if script == DockerEnvironment._LIVE_GROUPS_SCRIPT:
            running = answers.pop(0) if len(answers) > 1 else answers[0]
            return (0, "4242\n4242\n" if running else "")   # one line per live member
        return (0, "")
    return stub




def _start_two(env) -> None:
    """Start two background processes: colproc-1 (a watcher, say) and
    colproc-2 (the server)."""
    env.exec_background("npm run watch")
    env.exec_background("npm run dev")














def test_a_stop_forgets_the_groups_whose_grace_is_over__F1966(native_bridge, monkeypatch):
    """What a stop records for a teardown to wait for holds only the groups
    still inside their grace, so a long serve whose parent starts and
    stops many processes never carries more than the last few."""
    DockerEnvironment = _docker_env_module().DockerEnvironment
    monkeypatch.setattr(DockerEnvironment, "_STOP_GRACE_S", 0.05)
    env = _make_env(native_bridge)
    groups = iter(range(5000, 5010))
    native_bridge.stub_env_exec(
        lambda _id, cmd, kw: (0, f"sigterm:{next(groups)}\n") if _step(
            {"op": "env_exec", "command": cmd}) == "stop" else (0, ""))
    for _ in range(3):
        env.stop_process(env.exec_background("npm run dev"))
        time.sleep(0.06)
    env.stop_process(env.exec_background("npm run dev"))
    assert list(env._stopping) == ["5003"]








def test_a_stop_that_waits_takes_a_probe_that_raises_for_one_that_could_not_run__F1966(
        native_bridge, monkeypatch):
    """The oneshot server's stop waits too: a probe whose exec raises (the
    connection broke under it) is logged, and the rest of the grace waited
    out, rather than raised past the freeze that follows."""
    DockerEnvironment = _docker_env_module().DockerEnvironment
    monkeypatch.setattr(DockerEnvironment, "_STOP_GRACE_S", 0.3)
    group = _group_4242([True])

    def broken_probe(_id, cmd, kw):
        if _step({"op": "env_exec", "command": cmd}).startswith("probe"):
            raise RuntimeError("host closed IPC channel while awaiting ack")
        return group(_id, cmd, kw)

    native_bridge.stub_env_exec(broken_probe)
    env = _make_env(native_bridge)
    token = env.exec_background("npm run dev")
    before = len(native_bridge.env_ops)
    started = time.monotonic()
    env.stop_process(token, wait=True)
    assert time.monotonic() - started >= 0.3
    assert [_step(op) for op in native_bridge.env_ops[before:]] == ["stop", "probe 4242"]
    assert [line for line in native_bridge._rec_logs
            if "could not check whether background process group 4242 exited" in line
            and "host closed IPC channel" in line]


class _Clock:
    """The clock of ``_env_shared`` in a timing test, in place of its
    ``time`` module: monotonic time that moves only when the code under
    test sleeps or an exec stub spends its round trip (:meth:`sleep`), so
    what the test measures is exact whatever this machine's speed."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def test_the_teardown_gives_its_own_groups_their_whole_grace_beside_a_routed_one__F1966(
        native_bridge, monkeypatch):
    """Each group a teardown waits for keeps its own end. The parent
    stopped one process half a grace before the teardown, and its group
    never exits; the teardown stops the other itself, and that group
    exits a moment after the first one's grace is over. The teardown
    polls the parent's group to the end of its grace (logged as still
    running, then polled no more) and its own until it has exited, well
    inside its own grace, and only then freezes. On a :class:`_Clock`,
    each exec taking 0.01 s."""
    mod = _docker_env_module()
    monkeypatch.setattr(mod.DockerEnvironment, "_STOP_GRACE_S", 1.0)
    clock = _Clock()
    monkeypatch.setattr(mod, "time", clock)
    env = _make_env(native_bridge)
    probes: list = []
    frozen: list = []

    def two_groups(_id, cmd, kw):
        clock.sleep(0.01)
        step = _step({"op": "env_exec", "command": cmd})
        if step == "stop":
            return (0, "sigterm:4242\n" if "/tmp/colproc-1.pid" in cmd[2] else "sigterm:4343\n")
        if step.startswith("probe"):
            probes.append((step.split()[1:], clock.now))
            own_runs = clock.now < env._stopping["4242"] + 0.1
            return (0, "4242\n" + ("4343\n" if own_runs else ""))
        if step == "freeze":
            frozen.append(clock.now)
        return (0, "")

    native_bridge.stub_env_exec(two_groups)
    _start_two(env)
    env.stop_process("colproc-1")
    clock.sleep(0.5)
    env._terminate(list(env._procs))
    env._await_exit()
    env.freeze()
    parents_end, own_end = env._stopping["4242"], env._stopping["4343"]
    [frozen_at] = frozen
    assert parents_end + 0.1 <= frozen_at < own_end
    assert [line for line in native_bridge._rec_logs if "still running" in line] == [
        "background process group 4242 still running 1 s after SIGTERM"]
    assert len([at for groups, at in probes if "4242" in groups and at >= parents_end]) == 1


def test_a_teardown_that_cannot_probe_waits_for_the_latest_grace__F1966(native_bridge,
                                                                       monkeypatch):
    """A liveness probe the host could not run leaves the teardown to wait
    out the grace of every group it waits for: to the latest of their
    ends (two groups stopped 0.2 s apart here), not the earliest, which
    would freeze the later group inside its grace. One probe, and no exec
    after it. On a :class:`_Clock`, each exec taking 0.01 s."""
    mod = _docker_env_module()
    monkeypatch.setattr(mod.DockerEnvironment, "_STOP_GRACE_S", 0.4)
    clock = _Clock()
    monkeypatch.setattr(mod, "time", clock)
    env = _make_env(native_bridge)
    groups = iter(["4242", "4343"])

    def no_probe(_id, cmd, kw):
        clock.sleep(0.01)
        step = _step({"op": "env_exec", "command": cmd})
        if step == "stop":
            return (0, f"sigterm:{next(groups)}\n")
        if step.startswith("probe"):
            return {"ok": False, "error": "exec failed: the container is restarting"}
        return (0, "")

    native_bridge.stub_env_exec(no_probe)
    _start_two(env)
    env.stop_process("colproc-1")
    clock.sleep(0.2)
    env.stop_process("colproc-2")
    before = len(native_bridge.env_ops)
    env._await_exit()
    assert [_step(op) for op in native_bridge.env_ops[before:]] == ["probe 4242 4343"]
    assert clock.now == pytest.approx(env._stopping["4343"])
    assert env._stopping["4343"] - env._stopping["4242"] == pytest.approx(0.21)
    assert [line for line in native_bridge._rec_logs if line.startswith(
        "could not check whether background process group 4242, 4343 exited (exit -1)")]


def test_a_stop_the_host_could_not_run_reaches_no_group__F1966(native_bridge):
    """A stop exec the host could not run answers with the host's error,
    which reports no group whatever numbers it holds: nothing is recorded
    for a teardown to wait for, and nothing is polled."""
    env = _make_env(native_bridge)
    token = env.exec_background("npm run dev")
    native_bridge.stub_env_exec(
        lambda _id, cmd, kw: {"ok": False, "error": "container 4242 is not running"})
    before = len(native_bridge.env_ops)
    env.stop_process(token, wait=True)
    assert [_step(op) for op in native_bridge.env_ops[before:]] == ["stop"]
    assert env._stopping == {}


def test_stop_process_waits_for_the_group_only_when_asked__F1966(native_bridge):
    """A parent's stop answers once the SIGTERM is sent, as before; a
    teardown's (``wait=True``, the oneshot server's) returns once the
    group has exited. Either way the process is forgotten, and a group
    the SIGTERM did not reach (already gone) is not polled."""
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(_group_4242([True, True, False]))
    first = env.exec_background("npm run dev")
    second = env.exec_background("npm run worker")
    env.stop_process(first)
    assert [_step(op) for op in native_bridge.env_ops][-1] == "stop"
    assert first not in env._procs
    before = len(native_bridge.env_ops)
    env.stop_process(second, wait=True)
    assert [_step(op) for op in native_bridge.env_ops[before:]] == [
        "stop", "probe 4242", "probe 4242", "probe 4242"]
    assert env._procs == {}
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, ""))
    third = env.exec_background("sleep 1")
    before = len(native_bridge.env_ops)
    env.stop_process(third, wait=True)
    assert [_step(op) for op in native_bridge.env_ops[before:]] == ["stop"]


def test_the_stop_signals_nothing_but_a_group_leaders_pid__F1966(native_bridge, tmp_path):
    """The stop's real script, run here with `kill` replaced by a shell
    function that only reports what it would signal: a pidfile holding a
    process's pid reaches its group; an empty one (the launcher has not
    written it yet), 0, 1 (`kill -- -1` would reach every process in the
    container) or garbage signal nothing. It reports each group it
    reached ("sigterm:<pid>") and removes the pid and rc files either
    way."""
    import shlex
    import subprocess

    env = _make_env(native_bridge)
    seen: list = []
    native_bridge.stub_env_exec(lambda _id, cmd, kw: seen.append(cmd) or (0, ""))
    run_dir, signals = tmp_path / "run", tmp_path / "signals"
    run_dir.mkdir()
    contents = {"a": "4242\n", "b": "", "c": "0\n", "d": "1\n", "e": "-7\n", "f": "x1\n"}
    for name, text in contents.items():
        (run_dir / f"{name}.pid").write_text(text)
        (run_dir / f"{name}.rc").write_text("")
        env._procs[name] = {"pidfile": str(run_dir / f"{name}.pid"),
                            "rcfile": str(run_dir / f"{name}.rc"), "started": 0}
    assert env._terminate(list(contents)) == []            # the stub answered no group
    [argv] = seen
    fake_kill = f'kill() {{ echo "signal $*" >> {shlex.quote(str(signals))}; }}; '
    done = subprocess.run(["sh", "-c", fake_kill + argv[2]], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["sigterm:4242"]
    assert signals.read_text() == "signal -- -4242\n"
    assert not any(run_dir.iterdir())
    assert env._procs == {}


def test_a_stop_whose_cleanup_failed_still_waits_for_the_group_it_signalled__F1966(
        native_bridge, tmp_path):
    """The stop's real script, run here with `kill` replaced by a shell
    function that only reports what it would signal. It signals the
    group, then its rm of the pid and rc files fails (the rc file is a
    directory here), so it exits 1; yet the SIGTERM did reach the group,
    so that group is recorded and waited for like any other."""
    import shlex
    import subprocess

    run_dir, signals = tmp_path / "run", tmp_path / "signals"
    run_dir.mkdir()
    (run_dir / "a.pid").write_text("4242\n")
    (run_dir / "a.rc").mkdir()
    fake_kill = f'kill() {{ echo "signal $*" >> {shlex.quote(str(signals))}; }}; '
    codes: list = []

    def here(_id, cmd, kw):
        if _step({"op": "env_exec", "command": cmd}) != "stop":
            return (0, "")                      # the probe: the group has exited
        done = subprocess.run(["sh", "-c", fake_kill + cmd[2]], capture_output=True,
                              text=True, timeout=30)
        codes.append(done.returncode)
        return (done.returncode, done.stdout + done.stderr)

    native_bridge.stub_env_exec(here)
    env = _make_env(native_bridge)
    env._procs["a"] = {"pidfile": str(run_dir / "a.pid"), "rcfile": str(run_dir / "a.rc"),
                       "started": 0}
    before = len(native_bridge.env_ops)
    env.stop_process("a", wait=True)
    assert codes == [1]
    assert signals.read_text() == "signal -- -4242\n"
    assert list(env._stopping) == ["4242"]
    assert [_step(op) for op in native_bridge.env_ops[before:]] == ["stop", "probe 4242"]


# The container's PID 1 in the tests that run background processes for
# real. Like the base image's (`sleep infinity`, no init) it never reaps: a
# process orphaned into it (exec_background's wrapper once the launcher
# has exited, the wrapper's child once the wrapper has) stays a zombie
# after it exits. It runs the launcher it is given, says so, then waits to
# be killed (every process these tests start ends on its own within two
# minutes, should a run be killed before its cleanup).
_PID_1 = (
    "import ctypes, subprocess, sys, time\n"
    "ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)   # PR_SET_CHILD_SUBREAPER\n"
    "subprocess.run(['sh', '-c', sys.argv[1]])\n"
    "print('launched', flush=True)\n"
    "time.sleep(120)\n"
)


class _Container:
    """This machine as a container, as an env_exec stub: exec_background's
    own launcher runs under a PID 1 that never reaps (:data:`_PID_1`), with
    the container's /tmp (the pid and rc files) moved into ``run_dir``; the
    stop and liveness scripts run as they are, ``on_probe(n)`` called
    before the n-th liveness probe. The freeze is recorded, with what
    ``on_freeze()`` returns then, and never run (`kill -STOP -1` here
    would stop every process of this user); any other exec is refused and
    recorded. :meth:`close` kills every process group it launched, then
    the PID 1s, whose zombies init then reaps."""

    def __init__(self, run_dir, on_freeze, on_probe=None):
        self.run_dir = str(run_dir)
        self.on_freeze = on_freeze
        self.on_probe = on_probe
        self.probes = 0
        self.at_freeze: list = []
        self.refused: list = []
        self.leaders: list = []
        self._pid_1s: list = []

    def __call__(self, _id, cmd, kw):
        import subprocess
        import sys

        cmd = [c.replace("/tmp/colproc-", f"{self.run_dir}/colproc-") for c in cmd]
        script = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
        step = _step({"op": "env_exec", "command": cmd})
        if kw.get("detach") and script.startswith("setsid sh -c "):
            pid_1 = subprocess.Popen([sys.executable, "-c", _PID_1, script],
                                     stdout=subprocess.PIPE, text=True)
            self._pid_1s.append(pid_1)
            if pid_1.stdout.readline() != "launched\n":
                return (-1, "the launcher did not run")
            pidfile = script.rsplit("echo $! > ", 1)[1].strip()
            with open(pidfile, encoding="utf-8") as fh:
                self.leaders.append(int(fh.read()))
            return (0, "")
        if step == "freeze":
            self.at_freeze.append(self.on_freeze())
            return (0, "")
        if step.startswith("probe"):
            self.probes += 1
            if self.on_probe is not None:
                self.on_probe(self.probes)
            return _run_here(cmd)
        if step == "stop":
            return _run_here(cmd)
        self.refused.append(cmd)
        return (-1, "not run on this machine")

    def close(self) -> None:
        import signal

        for leader in self.leaders:
            try:
                os.killpg(leader, signal.SIGKILL)   # its zombies hold the group id until then
            except OSError:
                pass
        for pid_1 in self._pid_1s:
            pid_1.kill()
            pid_1.wait(timeout=10)
            pid_1.stdout.close()


_needs_proc = pytest.mark.skipif(not os.path.exists("/proc/self/task"),
                                 reason="no /proc to read process states from")


@pytest.fixture
def container(tmp_path):
    """``container(on_freeze, on_probe)``: a :class:`_Container` whose /tmp
    is a directory under ``tmp_path``, closed when the test ends."""
    made: list = []

    def make(on_freeze, on_probe=None):
        run_dir = tmp_path / f"container-{len(made)}"
        run_dir.mkdir()
        made.append(_Container(run_dir, on_freeze, on_probe))
        return made[-1]

    try:
        yield make
    finally:
        for box in made:
            box.close()


def _rewrites_on_sigterm(tmp_path):
    """A background command whose SIGTERM handler rewrites ``state.txt``
    the way a program saving its state does: truncate it, work, write it
    whole. The work lasts until ``go`` exists, which the container
    creates at the teardown's second liveness probe: any teardown that
    waits for the group sees it running at least once, then gone, so the
    handler is certainly mid-rewrite at the first probe, however long one
    probe of this machine's processes takes, and a teardown that freezes
    without waiting for it freezes it halfway. Returns the command, the
    state file (holding "v1 complete state"), the file it creates once
    the handler is set, and ``on_probe`` for :func:`container`."""
    import shlex

    state, ready, go = tmp_path / "state.txt", tmp_path / "ready", tmp_path / "go"
    state.write_text("v1 complete state\n")
    q = shlex.quote(str(state))
    handler = (f": > {q}; for i in $(seq 3000); do [ -e {shlex.quote(str(go))} ] && break; "
               f"sleep 0.02; done; printf 'v2 complete state\\n' > {q}; exit 0")
    command = (f"trap {shlex.quote(handler)} TERM; : > {shlex.quote(str(ready))}; "
               "sleep 60 & wait")
    return command, state, ready, lambda n: n == 2 and go.touch()


def _wait_for(path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.01)


def _proc_state(pid) -> str:
    """A process's state letter, from its /proc stat line."""
    with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
        return fh.read().rsplit(") ", 1)[1].split()[0]


@_needs_proc
def test_the_teardown_lets_a_real_sigterm_handler_finish_its_rewrite__F1966(native_bridge,
                                                                             container,
                                                                             tmp_path,
                                                                             monkeypatch):
    """The teardown for real, against a background process started as
    exec_background starts one, in a stand-in container (:class:`_Container`):
    the wrapper leads the process group and is the pid its stop signals,
    and the command's SIGTERM handler (truncate a file, work, write it
    whole: :func:`_rewrites_on_sigterm`) runs in the wrapper's child. The
    wrapper dies at the SIGTERM and stays a zombie, nothing reaping it, so
    the teardown waits only because it counts every live member of the
    group, not the leader alone, and stops waiting only because it counts
    a zombie as gone. The freeze is only recorded, never run: at that
    moment the file is whole, inside the stop grace."""
    DockerEnvironment = _docker_env_module().DockerEnvironment
    monkeypatch.setattr(DockerEnvironment, "_STOP_GRACE_S", 30.0)   # returns as soon as it is gone
    command, state, ready, on_probe = _rewrites_on_sigterm(tmp_path)
    box = container(state.read_text, on_probe)
    native_bridge.stub_env_exec(box)
    env = _make_env(native_bridge)
    env.exec_background(command)
    _wait_for(ready)
    [leader] = box.leaders
    settled: list = []
    started = time.monotonic()
    env._terminate(list(env._procs))
    env._await_exit()
    env.freeze()
    settled.append((True, list(box.at_freeze)))
    took = time.monotonic() - started
    assert box.refused == []
    assert box.at_freeze == ["v2 complete state\n"]
    assert settled == [(True, box.at_freeze)]
    assert took < DockerEnvironment._STOP_GRACE_S, took
    assert _proc_state(leader) == "Z"              # the leader it counted as gone
    assert not any("still running" in line for line in native_bridge._rec_logs)




@_needs_proc
def test_the_probe_reads_the_group_of_a_process_whose_name_holds_a_paren__F1966(tmp_path):
    """A /proc stat line puts the command name in parentheses, and a name
    may itself hold ") ": the probe takes the state and the group from
    after the last one, so a process named "a) S 1 1 (c" is still found
    running in its group."""
    import signal
    import subprocess
    import sys

    DockerEnvironment = _docker_env_module().DockerEnvironment
    ready = tmp_path / "ready"
    code = ("import ctypes, sys, time\n"
            "ctypes.CDLL(None).prctl(15, b'a) S 1 1 (c', 0, 0, 0)   # PR_SET_NAME\n"
            "open(sys.argv[1], 'w').close()\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(ready)], start_new_session=True)
    try:
        _wait_for(ready)
        with open(f"/proc/{proc.pid}/comm", encoding="utf-8") as fh:
            assert fh.read() == "a) S 1 1 (c\n"
        rc, out = _run_here(["sh", "-c", DockerEnvironment._LIVE_GROUPS_SCRIPT, "sh",
                             str(proc.pid)])
        assert rc == 0 and out.split() == [str(proc.pid)], out
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


@_needs_proc
def test_the_probe_reads_the_group_of_a_process_whose_name_holds_a_line_break__F1966(
        tmp_path):
    """A /proc stat file holds the command name as it is, line breaks
    included, so its first line, the one the probe reads first, can end
    inside the name: holding no ") " at all, or what reads as a zombie's
    fields after one. The probe then reads the whole file, so processes
    named "a\\nZ 1 1 (c" and "a) Z 1 4\\nb" are still found running, each
    in its own group."""
    import signal
    import subprocess
    import sys

    DockerEnvironment = _docker_env_module().DockerEnvironment
    names = [b"a\nZ 1 1 (c", b"a) Z 1 4\nb"]
    procs: list = []
    try:
        for n, name in enumerate(names):
            ready = tmp_path / f"ready-{n}"
            code = ("import ctypes, sys, time\n"
                    f"ctypes.CDLL(None).prctl(15, {name!r}, 0, 0, 0)   # PR_SET_NAME\n"
                    "open(sys.argv[1], 'w').close()\n"
                    "time.sleep(60)\n")
            procs.append(subprocess.Popen([sys.executable, "-c", code, str(ready)],
                                          start_new_session=True))
            _wait_for(ready)
            with open(f"/proc/{procs[-1].pid}/comm", "rb") as fh:
                assert fh.read() == name + b"\n"
        pids = sorted(str(proc.pid) for proc in procs)
        rc, out = _run_here(["sh", "-c", DockerEnvironment._LIVE_GROUPS_SCRIPT, "sh", *pids])
        assert rc == 0 and sorted(set(out.split())) == pids, out
    finally:
        for proc in procs:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)


@_needs_proc
def test_the_probe_counts_a_process_whose_main_thread_exited__F1966(tmp_path):
    """A process whose main thread has exited while another of its threads
    runs reads as a zombie in its /proc stat line; a SIGTERM handler
    running in that other thread would be frozen halfway if the probe
    took the process for gone. So the probe reads every thread's state."""
    import signal
    import subprocess
    import sys

    DockerEnvironment = _docker_env_module().DockerEnvironment
    ready = tmp_path / "ready"
    code = ("import ctypes, sys, threading, time\n"
            "def work():\n"
            "    open(sys.argv[1], 'w').close()\n"
            "    time.sleep(60)\n"
            "threading.Thread(target=work).start()\n"
            "ctypes.CDLL(None).pthread_exit(None)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(ready)], start_new_session=True)
    try:
        _wait_for(ready)
        deadline = time.monotonic() + 10
        while _proc_state(proc.pid) != "Z":         # the main thread has exited
            assert time.monotonic() < deadline
            time.sleep(0.01)
        rc, out = _run_here(["sh", "-c", DockerEnvironment._LIVE_GROUPS_SCRIPT, "sh",
                             str(proc.pid)])
        assert rc == 0 and out.split() == [str(proc.pid)], out
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)




# The host's cap on one exec's output, and what it returns past it: the
# tail behind a marker naming how many earlier bytes it dropped
# (api/process_manager/environments.py).
_HOST_EXEC_CAP = 8_000_000


def _host_tail(out: str) -> str:
    if len(out) <= _HOST_EXEC_CAP:
        return out
    kept = out[-(_HOST_EXEC_CAP - 64):]
    return f"[... {len(out) - len(kept)} earlier bytes of output dropped]\n" + kept


def _run_here(cmd):
    """Run an exec's argv on this machine, as the container would."""
    import subprocess

    done = subprocess.run(cmd, capture_output=True, timeout=60)
    out = (done.stdout + done.stderr).decode("utf-8", "replace")
    return done.returncode, _host_tail(out)


def test_read_file_carries_a_file_past_the_hosts_output_cap_whole__F1669(native_bridge, tmp_path):
    """The host keeps only the tail of an exec's output past its cap, so a
    whole document read through ``cat`` loses its head (a day-long agent
    session's record, a context file written back). ``read_file`` reads it
    in chunks, one exec each, and returns every byte."""
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: _run_here(cmd))
    big = tmp_path / "rollout.jsonl"
    data = os.urandom(_HOST_EXEC_CAP + 1_000_003)
    big.write_bytes(data)

    rc, cut = env.exec(["cat", str(big)])
    assert cut.startswith("[... ") and len(cut) <= _HOST_EXEC_CAP   # what cat gets

    before = len(native_bridge.env_ops)
    assert env.read_file(str(big)) == data
    reads = [o for o in native_bridge.env_ops[before:] if o["op"] == "env_exec"]
    assert len(reads) == 3                  # 4 MB, 4 MB, the rest
    # A file that fills its last chunk exactly ends on an empty one.
    exact = tmp_path / "exact"
    exact.write_bytes(b"x" * env._READ_CHUNK)
    assert env.read_file(str(exact)) == b"x" * env._READ_CHUNK
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert env.read_file(str(empty)) == b""


def test_read_file_fails_loudly_rather_than_partly__F1669(native_bridge, tmp_path):
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: _run_here(cmd))
    with pytest.raises(FileNotFoundError):
        env.read_file(str(tmp_path / "missing"))
    with pytest.raises(OSError, match="exit 4"):
        env.read_file(str(tmp_path))       # a directory: not a regular file
    # A chunk the host cut (its marker, then a tail) is never decoded as
    # data, even where the marker's letters and the tail would decode.
    native_bridge.stub_env_exec(
        lambda _id, cmd, kw: (0, "[... 12 earlier bytes of output dropped]\nQUJ\n"))
    with pytest.raises(OSError, match="did not arrive whole"):
        env.read_file("/any")
    native_bridge.stub_env_exec(lambda _id, cmd, kw: {"ok": False, "error": "environment closed"})
    with pytest.raises(RuntimeError, match="environment closed"):
        env.read_file("/any")


def _chunks(*answers):
    """An exec stub answering read_file's chunk reads with ``answers`` in
    turn: ``(header, data)`` pairs, sent as the header line (followed by
    the asking read's token, unless it names one itself) and the data's
    base64."""
    import base64

    queue = list(answers)

    def stub(_id, cmd, kw):
        header, data = queue.pop(0)
        if len(header.split()) == 2:
            header += f" {cmd[-1]}"
        return (0, f"{header}\n{base64.b64encode(data).decode()}\n")
    return stub


def test_read_file_takes_no_short_or_stray_answer_for_the_file__F1669(native_bridge, monkeypatch):
    """Each chunk names the read that asked for it and the offset it was
    read from, and the first one the file's size, so an answer that is not
    this chunk's — empty (a late answer to an exec that timed out, taken
    by this one), another offset's, another read's at the same offset
    (every read starts at @1), or short (a `tail` that died with nothing
    said) — raises rather than passing for the end of the file:
    inject_context_file would otherwise write the block alone, or another
    file, over a user's AGENTS.md. A file that grows while it is read (a
    live record) still arrives."""
    env = _make_env(native_bridge)
    monkeypatch.setattr(env, "_READ_CHUNK", 4)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, ""))
    with pytest.raises(OSError, match="did not arrive whole"):
        env.read_file("/w/AGENTS.md")
    native_bridge.stub_env_exec(_chunks(("@1 6", b"rule"), ("@1 6", b"rule")))
    with pytest.raises(OSError, match="did not arrive whole.*not @5"):
        env.read_file("/w/AGENTS.md")
    native_bridge.stub_env_exec(_chunks(("@1 3 0123456789abcdef", b"abc")))
    with pytest.raises(OSError, match="did not arrive whole.*not @1 of this read"):
        env.read_file("/w/AGENTS.md")
    native_bridge.stub_env_exec(_chunks(("@1 10", b"rule"), ("@5 -", b"")))
    with pytest.raises(OSError, match="4 of its 10 bytes arrived"):
        env.read_file("/w/AGENTS.md")
    native_bridge.stub_env_exec(_chunks(("@1 10", b"")))
    with pytest.raises(OSError, match="0 of its 10 bytes arrived"):
        env.read_file("/w/AGENTS.md")
    native_bridge.stub_env_exec(_chunks(("@1 -", b"")))
    with pytest.raises(OSError, match="did not arrive whole"):
        env.read_file("/w/AGENTS.md")
    native_bridge.stub_env_exec(_chunks(("@1 3", b"abcd"), ("@5 -", b"ef")))
    assert env.read_file("/w/rollout.jsonl") == b"abcdef"
    reads = [o["command"] for o in native_bridge.env_ops if o["op"] == "env_exec"][-2:]
    assert [argv[-3:-1] for argv in reads] == [["1", "4"], ["5", "4"]]
    # One token names every chunk of a read, and no other read.
    assert reads[0][-1] == reads[1][-1]
    native_bridge.stub_env_exec(_chunks(("@1 2", b"ab")))
    assert env.read_file("/w/rollout.jsonl") == b"ab"
    again = [o["command"] for o in native_bridge.env_ops if o["op"] == "env_exec"][-1]
    assert again[-1] != reads[0][-1]


def test_read_file_refuses_a_chunk_longer_than_it_asked_for__F1669(native_bridge, monkeypatch):
    env = _make_env(native_bridge)
    monkeypatch.setattr(env, "_READ_CHUNK", 4)
    native_bridge.stub_env_exec(_chunks(("@1 5", b"abcde")))
    with pytest.raises(OSError, match="longer than asked"):
        env.read_file("/w/f")


def test_one_read_chunk_fits_under_the_hosts_exec_cap__F1669(native_bridge, tmp_path):
    """read_file works only while one chunk's whole answer (its header
    line, then its base64 with the line breaks base64 puts every 76
    characters) is within what the host carries back before it keeps only
    the tail. Run for real on a file one chunk long."""
    env = _make_env(native_bridge)
    n = env._READ_CHUNK
    one = tmp_path / "one"
    one.write_bytes(b"\x00" * n)
    answers: list = []

    def here(_id, cmd, kw):
        answers.append((cmd[-1], *_run_here(cmd)))
        return answers[-1][1:]

    native_bridge.stub_env_exec(here)
    assert env.read_file(str(one)) == b"\x00" * n
    token, rc, out = answers[0]
    b64 = 4 * -(-n // 3)
    assert len(f"@{n + 1} {10 ** 15} {token}\n") + b64 + -(-b64 // 76) <= _HOST_EXEC_CAP
    assert rc == 0 and out.startswith(f"@1 {n} {token}\n") and len(out) <= _HOST_EXEC_CAP


def test_placement_failure_raises_the_hosts_reason(native_bridge):
    native_bridge.stub_env_place(lambda spec: {"ok": False, "error": "no docker here"})
    with pytest.raises(RuntimeError, match="no docker here"):
        _make_env(native_bridge)


def test_exec_failure_is_rc_minus_one_with_the_hosts_error(native_bridge):
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: {"ok": False, "error": "gone"})
    assert env.exec(["true"]) == (-1, "gone")


def test_close_is_idempotent_and_host_mediated(native_bridge):
    env = _make_env(native_bridge)
    env.close()
    env.close()
    closes = [o for o in native_bridge.env_ops if o["op"] == "env_close"]
    assert len(closes) == 1
    assert env.container_id == ""


# (The _ensure_image build-ordering tests moved with the logic: image
# ensure is host-side now, pinned by the app repo's EnvironmentService
# tests against a fake docker client.)


def test_readonly_files_are_declared_content_not_host_paths__D05(native_bridge):
    env = _make_env(native_bridge, readonly_files={'/policy/guard.json':'{"deny":true}'})
    try:
        placement = next(row for row in native_bridge.env_ops if row['op'] == 'env_place')
        assert placement['spec']['readonly_files'] == {'/policy/guard.json':'{"deny":true}'}
    finally:
        env.close()


def test_shared_base_and_layered_spec_change_with_base_content__F1649(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(_GEN_DIR)
    import _env_shared as shared

    base = tmp_path / "base"
    base.mkdir()
    (base / "Dockerfile").write_text("FROM debian:bookworm-slim\n")
    context = tmp_path / "image"
    context.mkdir()
    (context / "Dockerfile").write_text("ARG BASE\nFROM ${BASE}\n")
    monkeypatch.setattr(shared, "base_context", lambda: str(base))
    first = shared.layered_image_spec("fixture", str(context), build_args={"TOOL_VERSION": "1"})
    assert first["base_image"].startswith(f"collimate-{shared.BASE_NAME}:")
    assert first["build_args"] == {"TOOL_VERSION": "1", "BASE": first["base_image"]}
    assert first["base_context"] == str(base)
    (base / "Dockerfile").write_text("FROM debian:bookworm-slim\nRUN true\n")
    second = shared.layered_image_spec("fixture", str(context), build_args={"TOOL_VERSION": "1"})
    assert second["base_image"] != first["base_image"]
    assert second["image"] != first["image"]
    third = shared.layered_image_spec("fixture", str(context), build_args={"TOOL_VERSION": "2"})
    assert third["base_image"] == second["base_image"]
    assert third["image"] != second["image"]


@pytest.mark.parametrize('detached', [False, True])
def test_command_exec_forwards_existing_detach_and_preserves_identity_P4F31(native_bridge, detached):
    env = _make_env(native_bridge)
    native_bridge.stub_env_exec(lambda _id, cmd, kw: (0, '' if kw.get('detach') else 'captured'))
    options = {'detach': True} if detached else {}
    result = env.exec(['python', '-c', 'print("proof")'], cwd='/work/draft',
                      env={'PRIVATE': 'synthetic'}, user='1000:1000', **options)
    call = [row for row in native_bridge.env_ops if row['op'] == 'env_exec'][-1]
    assert call['detach'] is detached
    assert call['command'] == ['python', '-c', 'print("proof")']
    assert call['cwd'] == '/work/draft' and call['env'] == {'PRIVATE': 'synthetic'}
    assert call['user'] == '1000:1000'
    assert result == (0, '' if detached else 'captured')


def test_native_close_returns_host_acknowledgement_P4F47(native_bridge, monkeypatch):
    import gen
    acknowledgement = {"ok": False, "error": "container removal refused"}
    monkeypatch.setattr(native_bridge, "env_close", lambda env_id: acknowledgement)
    assert gen.close_environment("env-test") == acknowledgement


@pytest.mark.parametrize("ack", [
    {"ok": True, "error": None},
    {"ok": False, "error": "container removal refused"},
    {},
])
def test_close_returns_cached_status_and_logs_failed_ack_without_retry_P4F47(
    native_bridge, monkeypatch, ack,
):
    env = _make_env(native_bridge)
    calls = []
    monkeypatch.setattr(native_bridge, "env_close", lambda env_id: calls.append(env_id) or ack)
    status = env.close()
    expected = {"ok": bool(ack.get("ok")),
                "error": None if ack.get("ok") else ack.get("error") or "environment close was not acknowledged"}
    assert status == expected
    assert env.close() == expected
    assert len(calls) == 1
    assert env.container_id == ""
    failures = [line for line in native_bridge._rec_logs if "environment close failed" in line]
    assert len(failures) == (0 if expected["ok"] else 1)
    if failures:
        assert expected["error"] in failures[0]
        assert calls[0] in failures[0]


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "nak"])
def test_close_transport_failure_is_reported_without_retry_or_masking_body_P4F47(
    native_bridge, monkeypatch, failure,
):
    from collimate._host.bridge import IPCTimeoutError, IPCDisconnectedError, IPCNakError
    errors = {"timeout": IPCTimeoutError("env_close", 60),
              "disconnect": IPCDisconnectedError("connection closed"),
              "nak": IPCNakError("denied", "env_close")}
    env = _make_env(native_bridge)
    calls = []

    def close(env_id):
        calls.append(env_id)
        raise errors[failure]

    monkeypatch.setattr(native_bridge, "env_close", close)
    with pytest.raises(ValueError, match="original body failure"):
        try:
            raise ValueError("original body failure")
        finally:
            status = env.close()
    assert status == {"ok": False, "error": str(errors[failure])}
    assert env.close() == status
    assert len(calls) == 1
    assert sum("environment close failed" in line for line in native_bridge._rec_logs) == 1


def test_concurrent_close_waits_for_cached_status_without_second_request_P4F47(
    native_bridge, monkeypatch,
):
    """The second close arrives while the first is waiting for its ack."""
    import threading
    env = _make_env(native_bridge)
    awaiting_ack, release_ack, second_started, second_done = (
        threading.Event() for _ in range(4))
    calls, results, errors = [], [], []

    def acknowledge(env_id):
        calls.append(env_id)
        awaiting_ack.set()
        assert release_ack.wait(3)
        return {"ok": False, "error": "removal refused"}

    def close(second=False):
        if second:
            second_started.set()
        try:
            results.append(env.close())
        except BaseException as exc:
            errors.append(exc)
        finally:
            if second:
                second_done.set()

    monkeypatch.setattr(native_bridge, "env_close", acknowledge)
    first = threading.Thread(target=close)
    second = threading.Thread(target=close, args=(True,))
    first.start()
    try:
        assert awaiting_ack.wait(3)
        second.start()
        assert second_started.wait(3)
        # Hold the first ack until the competing close has had a chance to
        # observe the in-progress state. It must wait, not read an empty cache.
        assert not second_done.wait(0.1)
    finally:
        release_ack.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert results == [{"ok": False, "error": "removal refused"}] * 2
    assert len(calls) == 1
    assert sum("environment close failed" in line for line in native_bridge._rec_logs) == 1
