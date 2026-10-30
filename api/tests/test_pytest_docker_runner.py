"""Tests for api/process_manager/docker_runner.py + the ProcessManager.start()
runner selection.

Two layers:
  * unit (no docker) — runner selection by grant, the pure argv/config builders
    (mounts, run-id + instance labels, network, env forwarding, --user/--memory),
    network planning
    per internet mode, and the 0.0.0.0-bind IPC auth wrapper;
  * live (docker-gated) — a real containerized run of a probe generator through
    the whole create_run pipeline: it hits IPC, commits output, sees
    COLLIMATE_CONTAINERIZED=1, and leaves no container behind; plus force-stop
    reaping the container.

The live tests root the run under $HOME so snap-confined docker can bind-mount
the run's app/workspace dirs (docker can't see /tmp). Gated on a reachable
daemon (a hard skip, following test_sandbox.py's docker dependency).
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(THIS_DIR)
SDK_SRC = os.path.join(os.path.dirname(API_DIR), "sdk", "src")
for _p in (API_DIR, SDK_SRC, THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from process_manager.docker_runner import (  # noqa: E402
    RunnerSpec,
    build_run_argv,
    build_run_client_env,
    forward_env,
    plan_network,
)
from instance import INSTANCE_LABEL, instance_id  # noqa: E402
from runner_permissions import RunnerPermissions  # noqa: E402


# ---------------------------------------------------------------------------
# docker availability gate
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


DOCKER_OK = _docker_available()
requires_docker = pytest.mark.skipif(not DOCKER_OK, reason="docker daemon not reachable")


# ===========================================================================
# Unit — pure builders
# ===========================================================================

def test_runner_image_tracks_context_and_python__F0227_F0180(monkeypatch, tmp_path):
    from types import SimpleNamespace
    dr = importlib.import_module("process_manager.docker_runner")
    monkeypatch.setattr(dr, "runner_image_dir", lambda: str(tmp_path))
    recipe = tmp_path / "Dockerfile"
    recipe.write_text("FROM python:${PYTHON_VERSION}-slim\n")
    first = dr.default_runner_image()
    assert first.startswith(f"collimate-runner:py{sys.version_info.major}.{sys.version_info.minor}-")
    assert dr.default_runner_image() == first
    recipe.write_text("FROM python:${PYTHON_VERSION}-slim\n# changed\n")
    assert dr.default_runner_image() != first
    recipe.write_text("FROM python:${PYTHON_VERSION}-slim\n")
    assert dr.default_runner_image() == first
    monkeypatch.setattr(dr.sys, "version_info", SimpleNamespace(major=3, minor=14))
    assert dr.default_runner_image() != first


def test_runner_context_frames_paths_and_content__F0227(monkeypatch, tmp_path):
    dr = importlib.import_module("process_manager.docker_runner")
    monkeypatch.setattr(dr, "runner_image_dir", lambda: str(tmp_path))
    (tmp_path / "a").write_bytes(b"bc")
    first = dr.default_runner_image()
    (tmp_path / "a").unlink()
    (tmp_path / "ab").write_bytes(b"c")
    assert dr.default_runner_image() != first
    (tmp_path / "a").write_bytes(b"other")
    stable = dr.default_runner_image()
    # Directory creation order and mtimes cannot affect the recipe identity.
    data = (tmp_path / "ab").read_bytes()
    (tmp_path / "ab").unlink()
    (tmp_path / "ab").write_bytes(data)
    assert dr.default_runner_image() == stable


async def test_runner_ensure_receives_host_python_build_arg__F0227_F0180__P6F01(monkeypatch):
    from collections import deque
    from types import SimpleNamespace
    import sandbox
    dr = importlib.import_module("process_manager.docker_runner")
    captured = {}

    async def ensure(image, context, log, **kwargs):
        captured.update(image=image, context=context, **kwargs)
        raise _Sentinel()

    monkeypatch.setattr(sandbox, "ensure_image", ensure)
    manager = SimpleNamespace(run_id="image-policy", log_buffer=deque(),
                              permissions=RunnerPermissions.from_definition(
                                  {"docker": "limited", "internet": "full", "update_packages": False}))
    with pytest.raises(_Sentinel):
        await dr.start_containerized_run(manager, env={}, entrypoint="probe.py", manifest={})
    assert captured["build_args"] == {"PYTHON_VERSION": f"{sys.version_info.major}.{sys.version_info.minor}"}
    assert captured["image"] == dr.default_runner_image()
    assert captured["refresh"] is False


def test_plan_network_full_uses_default_bridge_no_proxy():
    p = plan_network("run1", "full", ())
    assert p.network == "bridge"
    assert p.per_run_network is None
    assert p.needs_proxy is False
    assert p.masquerade_off is False


def test_plan_network_proxy_makes_per_run_masq_off_net_with_allow():
    p = plan_network("run2", "proxy", ["anthropic.com"])
    assert p.per_run_network == "collimate-int-run2"
    assert p.network == "collimate-int-run2"
    assert p.masquerade_off is True
    assert p.needs_proxy is True
    assert p.allow == ("anthropic.com",)


def test_plan_network_none_forces_empty_allowlist():
    # Even if internet_allow carries hosts, `none` denies everything.
    p = plan_network("run3", "none", ["anthropic.com"])
    assert p.needs_proxy is True
    assert p.masquerade_off is True
    assert p.allow == ()


def test_forward_env_drops_host_only_and_docker_vars_keeps_collimate():
    env = {
        "COLLIMATE_RUN_ID": "r", "COLLIMATE_WORKSPACE": "/home/u/ws",
        "ANTHROPIC_API_KEY": "sk-x", "PYTHONPATH": "/home/u/sdk",
        "HOME": "/home/u", "PATH": "/usr/bin", "DOCKER_HOST": "unix:///x",
        "SSL_CERT_FILE": "/etc/ssl/host.pem", "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/home/u/.cache",
    }
    out = forward_env(env, {"COLLIMATE_CONTAINERIZED": "1"})
    assert out["COLLIMATE_RUN_ID"] == "r"
    assert out["COLLIMATE_WORKSPACE"] == "/home/u/ws"
    assert out["ANTHROPIC_API_KEY"] == "sk-x"           # secrets forwarded
    assert out["PYTHONPATH"] == "/home/u/sdk"           # SDK path forwarded
    assert out["COLLIMATE_CONTAINERIZED"] == "1"        # extra wins
    for denied in ("HOME", "PATH", "DOCKER_HOST", "SSL_CERT_FILE", "TMPDIR",
                   "XDG_CACHE_HOME"):
        assert denied not in out, denied


def test_build_run_argv_assembly__F0228():
    spec = RunnerSpec(
        image="collimate-runner:py3.13",
        container_name="collimate-run-RID",
        run_id="RID",
        app_dir="/home/u/.collimate/tmp/app_x",
        workspace_dir="/home/u/.collimate/tmp/ws_x",
        sdk_src="/home/u/sdk/src",
        entrypoint="agent.py",
        env={"COLLIMATE_RUN_ID": "RID", "DOCKER_HOST": "unix:///x"},
        network="bridge",
        user="1000:1000",
        memory_bytes=4 * 1024**3,
        pids_limit=2048,
    )
    argv = build_run_argv(spec, {"COLLIMATE_CONTAINERIZED": "1"})
    s = " ".join(argv)

    assert argv[:3] == ["docker", "run", "--rm"]  # no -i: generator uses TCP IPC, not stdin
    assert "--name collimate-run-RID" in s
    assert "--label com.collimate.run_id=RID" in s
    # The owner stamp keeps a SECOND live server's boot reaper (a dev run
    # beside the installed app, same dockerd) off this container.
    assert f"--label {INSTANCE_LABEL}={instance_id()}" in s
    assert "--network bridge" in s
    assert "--add-host host.docker.internal:host-gateway" in s
    assert "--user 1000:1000" in s
    assert f"--memory {4 * 1024**3}" in s
    assert "--pids-limit 2048" in s
    # Identical-path bind mounts (snap-docker reachability under $HOME).
    assert "-v /home/u/.collimate/tmp/app_x:/home/u/.collimate/tmp/app_x" in s
    assert "-v /home/u/.collimate/tmp/ws_x:/home/u/.collimate/tmp/ws_x" in s
    assert "-v /home/u/sdk/src:/home/u/sdk/src:ro" in s
    # Exactly the run/app/readonly SDK binds: no shared writable uv cache.
    assert [argv[i + 1] for i, part in enumerate(argv) if part == "-v"] == [
        f"{spec.app_dir}:{spec.app_dir}", f"{spec.workspace_dir}:{spec.workspace_dir}",
        f"{spec.sdk_src}:{spec.sdk_src}:ro",
    ]
    # Workdir = app_dir, launch = uv run <entrypoint>.
    assert "-w /home/u/.collimate/tmp/app_x" in s
    assert argv[-4:] == ["collimate-runner:py3.13", "uv", "run", "agent.py"]
    # Env forwarding: COLLIMATE_* + CONTAINERIZED in (by name; the values
    # ride in the client env), DOCKER_HOST dropped.
    assert "-e COLLIMATE_RUN_ID" in s and "COLLIMATE_RUN_ID=RID" not in s
    assert "-e COLLIMATE_CONTAINERIZED" in s
    assert "DOCKER_HOST" not in s
    client_env = build_run_client_env(spec, {"COLLIMATE_CONTAINERIZED": "1"})
    assert client_env["COLLIMATE_RUN_ID"] == "RID"
    assert client_env["COLLIMATE_CONTAINERIZED"] == "1"


def test_run_secrets_never_reach_the_docker_run_argv():
    # A process argv is world-readable in /proc/<pid>/cmdline for the whole
    # run; account keys and the IPC token must ride in the client env.
    spec = _minimal_spec(env={"OPENAI_API_KEY": "FAKE-ACCOUNT-KEY", "COLLIMATE_IPC_TOKEN": "FAKE-IPC"})
    argv = build_run_argv(spec, {"HTTP_PROXY": "http://host.docker.internal:9"})
    assert not any("FAKE-ACCOUNT-KEY" in part or "FAKE-IPC" in part for part in argv)
    client_env = build_run_client_env(spec, {"HTTP_PROXY": "http://host.docker.internal:9"})
    assert client_env["OPENAI_API_KEY"] == "FAKE-ACCOUNT-KEY"
    assert client_env["COLLIMATE_IPC_TOKEN"] == "FAKE-IPC"
    # A name the docker client reads itself stays inline, off its own env.
    assert "HTTP_PROXY=http://host.docker.internal:9" in argv
    assert client_env.get("HTTP_PROXY") != "http://host.docker.internal:9"


def test_server_auth_tokens_never_reach_a_container():
    out = forward_env({"COLLIMATE_TOKEN": "server-bearer", "COLLIMATE_CONNECT_TOKEN": "connect",
                       "COLLIMATE_RUN_ID": "r"})
    assert out == {"COLLIMATE_RUN_ID": "r"}


def _minimal_spec(**overrides):
    base = dict(
        image="collimate-runner:py3.13",
        container_name="collimate-run-RID",
        run_id="RID",
        app_dir="/home/u/.collimate/tmp/app_x",
        workspace_dir="/home/u/.collimate/tmp/ws_x",
        sdk_src="/home/u/sdk/src",
        entrypoint="agent.py",
        env={},
        network="bridge",
    )
    base.update(overrides)
    return RunnerSpec(**base)


def test_build_run_argv_dns_sink_for_restricted_networks():
    """A masquerade-off (restricted-internet) container gets ``--dns
    127.0.0.1`` — a dead in-container resolver — so Docker's embedded DNS
    can't recursively resolve external names via the HOST's stack (the
    DNS-tunnel exfiltration channel). Proxy-mode HTTP(S) is unaffected: the
    host-side CONNECT proxy resolves target hosts itself."""
    argv = build_run_argv(_minimal_spec(dns="127.0.0.1"))
    i = argv.index("--dns")
    assert argv[i + 1] == "127.0.0.1"
    assert "--dns" not in build_run_argv(_minimal_spec())


def test_build_run_argv_omits_optional_flags_when_unset():
    spec = RunnerSpec(
        image="img", container_name="c", run_id="R",
        app_dir="/home/u/a", workspace_dir="/home/u/w", sdk_src="/home/u/s",
        entrypoint="m.py", env={}, network="bridge",
        user=None, memory_bytes=None, pids_limit=None,
    )
    s = " ".join(build_run_argv(spec))
    assert "--user" not in s
    assert "--memory" not in s
    assert "--pids-limit" not in s


# ===========================================================================
# Unit — refined internet invariant (runners)
# ===========================================================================

def _validate_grant(permissions, **manifest_options):
    from types import SimpleNamespace
    from runners import validate_definition
    gi = {"id":"g", "main_config":"settings.set_yaml", "fields":[],
          "files_by_name":{"settings.set_yaml": SimpleNamespace(content="{}\n")},
          "manifest":{"id":"g", "main_config":"settings.set_yaml",
                      "account_requirements":{}, **manifest_options}}
    runner = {"id":"g", "name":"G", "description":"test", "generator":"g", "settings":{"settings.set_yaml":{}},
              "permissions":permissions, "accounts":{}, "tags":[]}
    return validate_definition(gi, runner, [])


def test_refined_internet_invariant_rejects_nested_native():
    from runners import RunnerError
    with pytest.raises(RunnerError, match="docker == limited"):
        _validate_grant({"docker":"full", "internet":"proxy"})


def test_refined_internet_invariant_allows_main_container():
    _validate_grant({"docker":"limited", "internet":"none"})


def test_requires_docker_target_needs_a_full_grant():
    from runners import RunnerError
    for level in ("none", "limited"):
        with pytest.raises(RunnerError):
            _validate_grant({"docker":level}, requires_docker=True)
    _validate_grant({"docker":"full"}, requires_docker=True)


def test_plain_target_is_unaffected_by_the_docker_gate():
    _validate_grant({"docker":"none"})


# ===========================================================================
# Unit — runner selection by grant (no docker; spawn stubbed to a sentinel)
# ===========================================================================

class _Sentinel(Exception):
    pass


def _make_manager(perms, monkeypatch):
    from models import Channel
    from state import state
    channel = Channel(name="Runner selection")
    monkeypatch.setattr(state, "channels", [channel])
    run_id = str(uuid.uuid4())
    root = Path(".collimate/tmp").resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = root / f"app_{run_id[:8]}_probe"
    app.mkdir()
    (app / "choice.generator").write_text("id: probe\nname: probe\nentrypoint: probe.py\n")
    (app / "probe.py").write_text("import gen\n")
    ws = root / f"ws_{run_id[:8]}_probe"
    ws.mkdir()
    from process_manager import ProcessManager
    mgr = ProcessManager(
        channel=str(channel.id),
        run_id=run_id,
        app_dir=str(app),
        workspace_dir=str(ws),
        entrypoint="probe.py",
        permissions=perms,
    )
    # Decouple from the channel key registry / AST scan — the selection path
    # doesn't need a real env.
    mgr._build_subprocess_env = lambda *_a, **_k: {}  # type: ignore[method-assign]
    return mgr


async def test_runner_selection_docker_limited_routes_to_container(monkeypatch, fresh_cwd):
    mgr = _make_manager(RunnerPermissions.from_definition({"docker": "limited", "internet": "full"}), monkeypatch)
    captured: dict = {}

    async def fake_container(manager, *, env, entrypoint, manifest):
        captured["container"] = True
        captured["env"] = dict(env)
        captured["bind"] = manager._ipc_server.sockets[0].getsockname()[0]
        raise _Sentinel()

    async def fake_native(*a, **k):
        captured["native"] = True
        raise _Sentinel()

    # Import the submodule explicitly and patch the OBJECT: after another
    # test file's api_app fixture pops + re-imports process_manager, the
    # fresh package has no docker_runner ATTRIBUTE until the submodule is
    # imported, so a string-path setattr cannot walk to it.
    dr = importlib.import_module("process_manager.docker_runner")
    monkeypatch.setattr(dr, "start_containerized_run", fake_container)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_native)

    with pytest.raises(_Sentinel):
        await mgr.start()
    try:
        assert captured.get("container") is True
        assert "native" not in captured
        assert captured["bind"] == "0.0.0.0"                       # widened bind for the container
        assert captured["env"]["COLLIMATE_IPC_HOST"] == "host.docker.internal"
        assert captured["env"].get("COLLIMATE_IPC_TOKEN")          # per-run token present
        assert captured["env"].get("COLLIMATE_IPC_PORT")
    finally:
        if mgr._ipc_server:
            mgr._ipc_server.close()


@pytest.mark.parametrize("docker_level", ["none", "full"])
async def test_runner_selection_native_for_none_and_nested(monkeypatch, fresh_cwd, docker_level):
    # full stays NATIVE this iteration (the internet invariant guarantees a
    # full tag is internet: full, so nothing forces its containerization).
    perms = RunnerPermissions.from_definition({"docker": docker_level, "internet": "full"})
    mgr = _make_manager(perms, monkeypatch)
    captured: dict = {}

    async def fake_container(manager, **k):
        captured["container"] = True
        raise _Sentinel()

    async def fake_native(*a, **k):
        captured["native"] = True
        captured["bind"] = mgr._ipc_server.sockets[0].getsockname()[0]
        raise _Sentinel()

    # Import the submodule explicitly and patch the OBJECT: after another
    # test file's api_app fixture pops + re-imports process_manager, the
    # fresh package has no docker_runner ATTRIBUTE until the submodule is
    # imported, so a string-path setattr cannot walk to it.
    dr = importlib.import_module("process_manager.docker_runner")
    monkeypatch.setattr(dr, "start_containerized_run", fake_container)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_native)

    with pytest.raises(_Sentinel):
        await mgr.start()
    try:
        assert captured.get("native") is True
        assert "container" not in captured
        assert captured["bind"] == "127.0.0.1"  # loopback for native
        assert "COLLIMATE_IPC_HOST" not in captured  # (not applicable — sanity)
        assert mgr._ipc_server is None  # the failed spawn closed it
    finally:
        if mgr._ipc_server:
            mgr._ipc_server.close()


# ===========================================================================
# Live — a real containerized run through create_run
# ===========================================================================


PROBE_ONESHOT = (
    # A brief bit of work: a real generator is never an instant no-op, and a
    # sub-~2s container can race the host's event-delivery tasks (stdout/stderr
    # drain + IPC accept) during its short life under loop contention — a
    # fast-oneshot timing artifact of the shared run pipeline, not the docker
    # runner. A few seconds of work puts the update_cell/finish past that window.
    "import gen, os, time\n"
    "async def run():\n"
    "    time.sleep(3)\n"
    "    gen.update_file('out.txt', 'CONTAINERIZED=' + os.environ.get('COLLIMATE_CONTAINERIZED',''))\n"
    "    await gen.commit()\n"
    "gen.main(run)\n"
)

PROBE_DAEMON = (
    "import gen, os, time\n"
    "async def run():\n"
    "    gen.update_file('out.txt', 'up')\n"
    "    await gen.commit()\n"
    "    time.sleep(120)\n"      # hold the container open until stopped
    "gen.main(run)\n"
)


@contextlib.contextmanager
def _home_app():
    """A fresh app + TestClient rooted under $HOME so snap docker can bind-mount
    the run's app/workspace dirs. Reloads the cwd-keyed api modules, like
    conftest.api_app, but under a home-based dir instead of pytest's /tmp."""
    from fastapi.testclient import TestClient
    from conftest import reset_api_modules

    home = Path.home().resolve()
    project_scratch = Path(API_DIR).parent / "tmp" / "p5" / "docker-fixtures"
    # Snap can mount HOME, but test runs do not belong in the user's app data.
    scratch = project_scratch if project_scratch.is_relative_to(home) else home
    scratch.mkdir(parents=True, exist_ok=True)
    directory = tempfile.TemporaryDirectory(prefix="collimate-test-", dir=scratch)
    workdir = directory.name
    runtime = Path(workdir) / "account-runtime"
    runtime.mkdir(mode=0o700)
    prev = os.getcwd()
    os.chdir(workdir)
    reset_api_modules()
    try:
        import accounts
        with patch.object(accounts, "_cli_root", lambda: runtime):
            main = importlib.import_module("main")
            with TestClient(main.app) as c:
                r = c.post("/api/channels", params={"name": "Starting"})
                assert r.status_code == 200, r.text
                c.channel_id = r.json()["id"]
                yield c
    finally:
        os.chdir(prev)
        directory.cleanup()
        reset_api_modules()


@pytest.fixture(scope="module")
def home_client():
    """One home-rooted TestClient shared by ALL live docker tests in this
    module. Deliberately module-scoped: each TestClient owns its own asyncio
    loop, and asyncio's subprocess reaping (the `docker run` child's
    process.wait()) is unreliable across a fresh loop per test — so the whole
    file drives its containerized runs through a single loop, matching the
    single-loop path that reaps cleanly. Skipped tests never build it."""
    with _home_app() as c:
        yield c


def _seed_input_cell(channel: str) -> str:
    from database import get_db_connection
    from models import Cell, Stack, VirtualFile
    from persistence import initialize_files
    from versions import synthesize_genesis_row
    from state import persist_state, state
    seed_run, cid = str(uuid.uuid4()), str(uuid.uuid4())
    files = [VirtualFile(name="in.txt", content="seed\n", language="plaintext")]
    conn = get_db_connection(channel)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')", (seed_run,))
        assert synthesize_genesis_row(conn, cid, seed_run, 1, files)
        initialize_files(conn, cid, files)
        conn.commit()
    finally:
        conn.close()
    ch = next(x for x in state.channels if x.id == state.active_channel_id)
    ch.main_stacks.append(Stack(cells=[Cell(id=uuid.UUID(cid))]))
    persist_state()
    return cid


def _run_status(client, channel, run_id) -> str | None:
    r = client.get(f"/api/channels/{channel}/runs/{run_id}/events_since?since_id=0")
    return r.json().get("status") if r.status_code == 200 else None


def _container_exists(run_id: str) -> bool:
    r = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.collimate.run_id={run_id}"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def _dump_logs(client, channel, cid) -> str:
    """The run's canonical logs.txt from run_artifacts (keyed by the newest
    run targeting the cell) — surfaced in a failure assertion so a real bug
    is diagnosable, not silent."""
    from database import get_db_connection

    conn = get_db_connection(channel)
    try:
        row = conn.execute(
            "SELECT ra.blob_hash FROM run_artifacts ra "
            "JOIN runs r ON r.id = ra.run_id "
            "WHERE r.target_cell_id=? AND ra.category='log' "
            "AND ra.filepath='logs.txt' ORDER BY r.created_at DESC LIMIT 1",
            (cid,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "(no logs.txt)"
    bh = row["blob_hash"]
    try:
        return Path(".collimate", "objects", bh[:2], bh[2:]).read_text()
    except Exception:
        return "(logs.txt unreadable)"


def _run_runner_to_completion(client, channel, runner, timeout_s=180):
    """Seed a fresh input cell, run the runner in place, poll to a terminal
    status. Returns (cid, run_id, status)."""
    cid = _seed_input_cell(channel)
    r = client.post(
        f"/api/channels/{channel}/runs",
        json={"runner": runner, "cell_id": cid},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    status = None
    for _ in range(timeout_s):
        time.sleep(1)
        status = _run_status(client, channel, run_id)
        if status in ("completed", "failed", "stopped"):
            break
    return cid, run_id, status


@requires_docker
def test_containerized_smoke_run_commits_and_cleans_up(home_client):
    from conftest import install_runner
    client = home_client
    channel = client.channel_id
    runner, _gen = install_runner(
        generator="probe_box",
        runner="probe-box",
        entrypoint_body=PROBE_ONESHOT,
        definition={"permissions": {"docker": "limited", "internet": "full"}},
    )

    # A real docker daemon under load can transiently fail a `docker run`; this
    # is an integration test against a live daemon, so allow a bounded retry
    # (the containerized run itself is deterministic — see the in-loop
    # reliability the docker_runner change was verified against).
    last = None
    for _attempt in range(3):
        cid, run_id, status = _run_runner_to_completion(client, channel, runner)
        if status == "completed":
            break
        last = (cid, run_id, status)
    else:
        cid, run_id, status = last
        assert status == "completed", (
            f"run did not complete after retries: {status}\n"
            f"{_dump_logs(client, channel, cid)}"
        )

    # The generator's IPC update_cell committed out.txt onto the cell, and
    # COLLIMATE_CONTAINERIZED=1 was visible inside the box.
    response = client.get(f"/api/channels/{channel}/cells/{cid}/files")
    assert response.status_code == 200, response.text
    files = {f["path"]: f for f in response.json()["files"] if not f["deleted"]}
    assert "out.txt" in files, f"committed files: {sorted(files)}\n{_dump_logs(client, channel, cid)}"
    assert files["out.txt"]["file_revision"] > 0
    response = client.get(f"/api/channels/{channel}/cells/{cid}/files/out.txt")
    assert response.status_code == 200, response.text
    assert response.text == "CONTAINERIZED=1"

    # --rm cleaned the container up on clean exit. Removal is asynchronous
    # relative to the run turning terminal (dockerd reaps after the exit is
    # observed), so poll briefly instead of asserting the instant snapshot.
    gone = False
    for _ in range(20):
        if not _container_exists(run_id):
            gone = True
            break
        time.sleep(0.5)
    assert gone, "container should be gone after a clean exit"


@requires_docker
def test_force_stop_reaps_container(home_client):
    from conftest import install_runner
    client = home_client
    channel = client.channel_id
    runner, _gen = install_runner(
        generator="probe_daemon",
        runner="probe-daemon",
        entrypoint_body=PROBE_DAEMON,
        definition={"permissions": {"docker": "limited", "internet": "full"}},
    )

    # Full-scenario retry: start a long-lived containerized run, wait for the
    # container to come up, force-stop it, and verify the run-id-label reaper
    # (routes.runs.helpers._reap_run_containers, on every _reap_process)
    # force-removes it. The reap runs on the app event loop, whose servicing of
    # a fast run's teardown can lag under heavy test-harness contention (a
    # shared-pipeline timing artifact — the reap itself is proven to fire at
    # ~t+1s when the loop is free); retry the whole scenario rather than flake.
    reaped = False
    for _attempt in range(3):
        cid = _seed_input_cell(channel)
        r = client.post(
            f"/api/channels/{channel}/runs",
            json={"runner": runner, "cell_id": cid},
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        up = False
        for _ in range(60):
            time.sleep(1)
            if _container_exists(run_id):
                up = True
                break
            if _run_status(client, channel, run_id) in ("failed", "stopped"):
                break  # transient start failure — retry on a fresh cell
        if not up:
            continue

        # Force-stop → SIGKILL the docker-run client; the container is orphaned
        # until the run-id-label reaper force-removes it.
        d = client.delete(f"/api/channels/{channel}/runs/{run_id}?force=true")
        assert d.status_code == 200, d.text

        for _ in range(60):
            time.sleep(1)
            if not _container_exists(run_id):
                reaped = True
                break
        if reaped:
            break

    assert reaped, "container should be reaped after a force-stop (after retries)"


async def test_a_restricted_run_gets_the_proxy_url_with_its_credential__F0405(monkeypatch, tmp_path):
    """The container's HTTP(S)_PROXY is the run's own proxy URL, credential
    included: without it every proxied request would get 407. No docker —
    the image, the network and the spawn are stubbed."""
    from collections import deque
    from types import SimpleNamespace
    import egress_proxy
    import sandbox

    dr = importlib.import_module("process_manager.docker_runner")
    started: list = []

    class Recorded(egress_proxy.EgressProxy):
        def __init__(self, *a, **k):
            super().__init__(*a, bind_host="127.0.0.1", on_deny=k.get("on_deny"))
            started.append(self)

    async def no_op(*_a, **_k):
        return None

    captured: dict = {}

    async def fake_spawn(*argv, **kwargs):
        captured["argv"] = list(argv)
        raise _Sentinel()

    monkeypatch.setattr(egress_proxy, "EgressProxy", Recorded)
    monkeypatch.setattr(sandbox, "ensure_image", no_op)
    monkeypatch.setattr(dr, "_create_network", no_op)
    monkeypatch.setattr(dr, "_remove_network", no_op)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    manager = SimpleNamespace(
        run_id="run-cred", app_dir=str(tmp_path / "gen"), workspace_dir=str(tmp_path / "ws"),
        optional_requirements=[], log_buffer=deque(),
        permissions=RunnerPermissions.from_definition(
            {"docker": "limited", "internet": "proxy", "internet_allow": ["example.com"]}),
    )
    with pytest.raises(_Sentinel):
        await dr.start_containerized_run(manager, env={}, entrypoint="gen.py", manifest={})
    [proxy] = started
    want = proxy.url(dr.HOST_GATEWAY_ALIAS)
    assert want.startswith("http://collimate:")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{name}={want}" in captured["argv"], name
    assert proxy._server is None  # unwound with the failed spawn
