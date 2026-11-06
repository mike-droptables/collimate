#!/usr/bin/env python3
"""Boot a built server environment the way the desktop shell does, wait for
it to answer and run generators on it, optionally with no network at all.

    python3 build/check_server_env.py <server> [--offline] [--json OUT]

``<server>`` is a tree build/server_env.py wrote (``dist/desktop/server``, or a
package's ``resources/server``). The check reads its manifest and re-hashes
the tree against it (and again once the runs are over, so neither the server
nor a run wrote into it), then runs ``<server>/bin/python3 -I -B -m
collimate_server --port N --data-dir D --server-env <server>`` in a scratch
home with nothing on PATH but the system's (after a trap ``uv`` that fails
any run reaching for it), and waits for ``/health`` (the version the manifest
names) and ``/ready`` (``status: ok``, the bundled trees ``ok`` against the
wheel's RECORD). Then, through the server's API as the app does it, it runs
two probes, each in a channel of its own (its cell imported, its package
added to Capabilities, a runner saved from its default config, a run polled
to completion), and reads back what each run wrote:

- the run probe (``build/run_probe/``), with no third-party dependency;
- the dependency probe (``build/dependency_probe/``), whose header pins a
  pydantic other than the one the server environment carries, so a run that
  saw the server's packages would import the wrong one (or, on another
  Python, fail on its compiled pydantic_core).

Each run must have been started by the environment's uv, on the
environment's interpreter (its ``sys.base_prefix`` is the environment, and
the run log says so), with a PYTHONPATH of one directory holding ``collimate``
and ``gen`` alone; the dependency probe's pydantic and pydantic_core must be
its own pin, from outside the environment.

``--offline`` (Linux) runs the server and its runs in a new network
namespace (``unshare --user --map-root-user --net``), whose only interface
is the loopback this script brings up, and first proves that an outside
address is unreachable from there. The dependency probe's one fetch happens
before that, with the network: the environment's uv resolves and caches the
probe's dependencies for the environment's interpreter (``uv sync
--script``) in the scratch home's cache, which the server's runs then use
(uv's index answers stay fresh for minutes, so the offline run resolves from
it). It exits 3 when the namespace cannot be made (no unshare, or
unprivileged user namespaces refused), so a caller can tell "cannot check
here" from "the check failed" (1). Without ``--offline`` the dependency
probe's run fetches its dependencies itself, as a first run does.

Standard library only, so a release runner needs nothing installed for it.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server_env  # noqa: E402

INSIDE = "COLLIMATE_CHECK_SERVER_ENV_INSIDE"
# The scratch home the outer process made (and warmed) for the inner one.
SCRATCH = "COLLIMATE_CHECK_SERVER_ENV_SCRATCH"
CANNOT = 3
BUDGET_SECONDS = 90
RUN_BUDGET_SECONDS = 180
WARM_BUDGET_SECONDS = 300
HOME_DIRS = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR")
SDK_PACKAGES = ["collimate", "gen"]


@dataclass(frozen=True)
class Probe:
    """A generator package under build/ the check runs, and the file it writes."""

    folder: str
    output: str

    @property
    def source(self) -> Path:
        return Path(__file__).resolve().parent / self.folder

    @property
    def definition(self) -> str:
        return f"{self.folder}/{self.folder}.generator"

    @property
    def name(self) -> str:
        return self.folder.replace("_", " ").capitalize()


RUN_PROBE = Probe("run_probe", "ran-with.json")
DEPENDENCY_PROBE = Probe("dependency_probe", "own-deps.json")


def pinned_pydantic() -> str:
    """The pydantic version the dependency probe's header pins."""
    found = re.search(r'"pydantic==([^"]+)"', (DEPENDENCY_PROBE.source / "main.py").read_text())
    if found is None:
        raise AssertionError("the dependency probe's header pins no pydantic")
    return found.group(1)


def loopback_up() -> None:
    """Bring ``lo`` up in this (new) network namespace: SIOCSIFFLAGS |= IFF_UP."""
    import fcntl
    siocgifflags, siocsifflags, iff_up = 0x8913, 0x8914, 0x1
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("16sh", b"lo", 0)
        flags = struct.unpack("16sh", fcntl.ioctl(probe, siocgifflags, request))[1]
        fcntl.ioctl(probe, siocsifflags, struct.pack("16sh", b"lo", flags | iff_up))


def outside_unreachable() -> str:
    """Why 1.1.1.1:443 cannot be reached from here; raises when it can."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    except OSError as error:
        return errno.errorcode.get(error.errno or 0, str(error))
    raise AssertionError("an outside address is reachable: the namespace has a network")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def call(base: str, method: str, route: str, body=None, *, raw: bytes | None = None, timeout: float = 60):
    """One API call; the decoded JSON answer, or AssertionError naming the
    route and what the server said."""
    data, headers = raw, {"Content-Type": "application/octet-stream"}
    if body is not None:
        data, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
    request = urllib.request.Request(base + route, data=data, method=method, headers=headers if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        said = error.read().decode(errors="replace")
        raise AssertionError(f"{method} {route}: {error.code} {error.reason}, answering {said!r}") from error
    return json.loads(text) if text else None


def scratch_home(root: Path) -> dict[str, str]:
    """The environment the server (and the warm-up) run with, under ``root``:
    the system's PATH after a trap ``uv`` that fails a run reaching for it
    rather than the environment's, and a home of its own."""
    trap = root / "trap-bin"
    if not trap.is_dir():
        trap.mkdir()
        (trap / "uv").write_text("#!/bin/sh\necho \"the uv on PATH ran ($0), not the environment's\" >&2\nexit 97\n")
        (trap / "uv").chmod(0o755)
    env = {"PATH": f"{trap}:/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    for name in HOME_DIRS:
        (root / name).mkdir(exist_ok=True)
        env[name] = str(root / name)
    return env


def warm(server: Path, root: Path) -> dict:
    """With the network: resolve and cache the dependency probe's
    dependencies for the environment's interpreter, with the environment's
    uv, in the cache the server's runs use (``$XDG_CACHE_HOME/uv``)."""
    started = time.monotonic()
    done = subprocess.run(
        [str(server / server_env.UV_IN_TREE), "sync", "--script", str(DEPENDENCY_PROBE.source / "main.py"),
         "--python", str(server / "bin" / "python3")],
        cwd=root, env=scratch_home(root), capture_output=True, text=True, timeout=WARM_BUDGET_SECONDS)
    if done.returncode != 0:
        raise AssertionError(f"warming uv's cache for the dependency probe failed:\n{done.stderr}")
    return {"seconds": round(time.monotonic() - started, 2), "said": done.stderr.strip().splitlines()[-3:]}


def run_probe(base: str, probe: Probe) -> dict:
    """Run ``probe`` on the server at ``base`` as the app would: a new
    channel, the probe's cell imported into it and its package added to
    Capabilities, a runner saved from the probe's default config, and one
    run into a second cell, polled until it is over. Returns what the run
    wrote, its id and status and the host's lines in its log."""
    channel = call(base, "POST", "/api/channels?name=" + urllib.parse.quote(probe.name), {})["id"]
    files = []
    for source in sorted(probe.source.iterdir()):
        if source.is_file():
            blob = call(base, "POST", f"/api/channels/{channel}/blob/stream", raw=source.read_bytes())["blob_hash"]
            files.append({"path": f"{probe.folder}/{source.name}", "blob_hash": blob})
    notes = call(base, "POST", f"/api/channels/{channel}/blob/stream", raw=b"# Probe runs\n")["blob_hash"]
    imported = call(base, "POST", f"/api/channels/{channel}/import", {"insert_index": None, "stacks": [
        {"name": probe.name, "cells": [{"name": probe.name, "files": files, "dirs": []}]},
        {"name": "Probe runs", "cells": [{"name": "Probe runs", "files": [{"path": "notes.md", "blob_hash": notes}],
                                          "dirs": []}]}]})
    source_cell, target_cell = imported["cell_ids"]
    request = {"source_cell_id": source_cell, "definition_path": probe.definition, "target_cell_id": None}
    preview = call(base, "POST", f"/api/channels/{channel}/packages/preview", request)
    call(base, "POST", f"/api/channels/{channel}/packages/add", request | {
        "expected_content_hash": preview["content_hash"], "expected_target_revision": preview["target_revision"]})
    generator = preview["id"]
    draft = call(base, "GET", f"/api/runner_generators/{generator}/configs/default")
    call(base, "POST", "/api/runners", {"runner": {**draft["runner"], "generator": generator, "id": generator,
                                                   "name": probe.name}, "files": draft["files"]})
    run_id = call(base, "POST", f"/api/channels/{channel}/runs", {"runner": generator, "cell_id": target_cell})["run_id"]
    started = time.monotonic()
    while True:
        events = call(base, "GET", f"/api/channels/{channel}/runs/{run_id}/events_since?since_id=0")
        if events.get("is_terminal"):
            break
        if time.monotonic() - started > RUN_BUDGET_SECONDS:
            raise AssertionError(f"the {probe.folder} run did not finish within {RUN_BUDGET_SECONDS}s: "
                                 f"{events.get('status')}")
        time.sleep(0.5)
    rows = events.get("events", [])
    log = "".join(str(e.get("payload", "")) for e in rows if e.get("type") in ("stdout", "stderr", "log"))
    if events.get("status") != "completed":
        raise AssertionError(f"the {probe.folder} run ended {events.get('status')}:\n{log}")
    paths = call(base, "GET", f"/api/channels/{channel}/cells/{target_cell}/file-paths")["paths"]
    written = [path for path in paths if path.rsplit("/", 1)[-1] == probe.output]
    if len(written) != 1:
        raise AssertionError(f"the {probe.folder} run wrote {len(written)} {probe.output} files: {paths}")
    quoted = "/".join(urllib.parse.quote(part) for part in written[0].split("/"))
    said = call(base, "GET", f"/api/channels/{channel}/cells/{target_cell}/files/{quoted}")
    return {"run_id": run_id, "status": events["status"], "seconds": round(time.monotonic() - started, 2),
            "file": written[0], "host_log": [str(e.get("payload", "")) for e in rows if e.get("type") == "log"],
            **said}


def inside(path: str, root: Path) -> bool:
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))


def check_run(probe: Probe, run: dict, server: Path, manifest: dict) -> None:
    """What every probe run must show: the environment's uv, its interpreter
    (and the run log naming it), the SDK alone on its PYTHONPATH."""
    uv = server / server_env.UV_IN_TREE
    if not run.get("uv") or os.path.realpath(run["uv"]) != os.path.realpath(uv):
        raise AssertionError(f"the {probe.folder} run was started by the uv at {run.get('uv')}, not {uv}")
    if os.path.realpath(run.get("python_home", "")) != os.path.realpath(server):
        raise AssertionError(f"the {probe.folder} run ran on the Python at {run.get('python_home')}, "
                             f"not the environment's ({server})")
    version = manifest["python"]["version"]
    if run.get("python") != version:
        raise AssertionError(f"the {probe.folder} run ran on Python {run.get('python')}, not {version}")
    named = f"Python {version}, the server environment's ({server / 'bin' / 'python3'})"
    if named not in run["host_log"]:
        raise AssertionError(f"the {probe.folder} run's log does not say {named!r}: {run['host_log']}")
    path = run.get("pythonpath") or []
    if len(path) != 1 or path[0].get("holds") != SDK_PACKAGES or not inside(path[0]["path"], server):
        raise AssertionError(f"the {probe.folder} run's PYTHONPATH is {path}, not one directory of the "
                             f"environment's holding {' and '.join(SDK_PACKAGES)} alone")


def server_pydantic(server: Path) -> str | None:
    """The pydantic version the environment carries for the server itself."""
    found = sorted(server.glob("lib/python*/site-packages/pydantic-*.dist-info"))
    return found[0].name.removeprefix("pydantic-").removesuffix(".dist-info") if found else None


def check_own_dependencies(run: dict, server: Path) -> None:
    """The dependency probe imported its own pydantic, not the environment's."""
    pin = pinned_pydantic()
    if run["pydantic"]["version"] != pin:
        raise AssertionError(f"the dependency probe imported pydantic {run['pydantic']['version']}, "
                             f"not its pin {pin}")
    for module in ("pydantic", "pydantic_core"):
        if inside(run[module]["file"], server):
            raise AssertionError(f"the dependency probe imported the environment's {module}: {run[module]['file']}")


def stop(child: subprocess.Popen) -> None:
    """End the server's session: SIGTERM, then SIGKILL after 20s."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=20)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def boot(server: Path, manifest: dict, root: Path) -> dict:
    env = scratch_home(root)
    port = free_port()
    command = [str(server / "bin" / "python3"), "-I", "-B", "-m", "collimate_server",
               "--port", str(port), "--data-dir", str(root / "data"), "--server-env", str(server)]
    log_path = root / "server.log"
    started = time.monotonic()
    runs = {}
    failed: AssertionError | None = None
    with log_path.open("wb") as log:
        child = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            health = None
            while True:
                if child.poll() is not None:
                    raise AssertionError(f"the server exited ({child.returncode})")
                try:
                    health = health or get_json(f"http://127.0.0.1:{port}/health")
                    ready = get_json(f"http://127.0.0.1:{port}/ready")
                    if ready.get("status") == "ok":
                        break
                except OSError:
                    pass
                if time.monotonic() - started > BUDGET_SECONDS:
                    raise AssertionError(f"no /ready ok within {BUDGET_SECONDS}s")
                time.sleep(0.2)
            elapsed = time.monotonic() - started
            for key, probe in (("run", RUN_PROBE), ("own_dependencies", DEPENDENCY_PROBE)):
                runs[key] = run_probe(f"http://127.0.0.1:{port}", probe)
        except AssertionError as error:
            failed = error
        finally:
            stop(child)
    # Read only once the server is gone: uvicorn logs a route's traceback
    # after the 500 has gone out, so a log read on the answer can miss it.
    if failed is not None:
        raise AssertionError(f"{failed}\n--- server log (all of it, read after the server stopped) ---\n"
                             f"{log_path.read_text(errors='replace')}") from failed
    assert health is not None  # /ready answered ok after /health did
    if health.get("version") != manifest["collimate"]["version"]:
        raise AssertionError(f"/health says version {health.get('version')}, the manifest "
                             f"{manifest['collimate']['version']}")
    if ready.get("checks", {}).get("bundled") != "ok":
        raise AssertionError(f"/ready reports the bundled trees as {ready.get('checks')}")
    check_run(RUN_PROBE, runs["run"], server, manifest)
    check_run(DEPENDENCY_PROBE, runs["own_dependencies"], server, manifest)
    check_own_dependencies(runs["own_dependencies"], server)
    runs["own_dependencies"]["server_pydantic"] = server_pydantic(server)
    return {"command": command, "health": health, "ready": ready, "seconds_to_ready": round(elapsed, 2), **runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("server", type=Path)
    parser.add_argument("--offline", action="store_true", help="run in a network namespace of its own (Linux)")
    parser.add_argument("--json", type=Path, help="write the result here")
    args = parser.parse_args(argv)
    server = args.server.resolve()
    is_inside = os.environ.get(INSIDE) == "1"
    if args.offline and not is_inside:
        unshare = shutil.which("unshare")
        if sys.platform != "linux" or unshare is None:
            print("check_server_env: no unshare here; the offline check needs Linux", file=sys.stderr)
            return CANNOT
        probe = subprocess.run([unshare, "--user", "--map-root-user", "--net", "true"], capture_output=True, text=True)
        if probe.returncode != 0:
            print(f"check_server_env: cannot make a network namespace: {probe.stderr.strip()}", file=sys.stderr)
            return CANNOT
        with tempfile.TemporaryDirectory(prefix="check-server-env-") as scratch:
            try:
                warmed = warm(server, Path(scratch))
            except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
                print(f"check_server_env: {error}", file=sys.stderr)
                return 1
            (Path(scratch) / "warmed.json").write_text(json.dumps(warmed))
            return subprocess.run([unshare, "--user", "--map-root-user", "--net", sys.executable, "-I",
                                   str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])],
                                  env={**os.environ, INSIDE: "1", SCRATCH: scratch}).returncode
    try:
        result = {"server": str(server), "offline": is_inside}
        if is_inside:
            loopback_up()
            result["outside"] = outside_unreachable()
        manifest = server_env.read_manifest(server)
        result["manifest"] = manifest
        with tempfile.TemporaryDirectory(prefix="check-server-env-") as fresh:
            root = Path(os.environ.get(SCRATCH) or fresh)
            if (root / "warmed.json").is_file():
                result["warmed"] = json.loads((root / "warmed.json").read_text())
            result.update(boot(server, manifest, root))
        # Neither the server nor a run wrote into the environment.
        server_env.read_manifest(server)
    except (AssertionError, server_env.Refused, OSError) as error:
        print(f"check_server_env: {error}", file=sys.stderr)
        return 1
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    where = f"offline (outside: {result['outside']})" if is_inside else "with the network"
    own = result["own_dependencies"]
    print(f"check_server_env: {server} ready in {result['seconds_to_ready']}s {where}, "
          f"version {result['health']['version']}, bundled {result['ready']['checks']['bundled']}; "
          f"generator runs by its uv {result['manifest']['uv']['version']} on its Python {result['run']['python']}: "
          f"the run probe {result['run']['status']} in {result['run']['seconds']}s, the dependency probe "
          f"{own['status']} in {own['seconds']}s with its own pydantic {own['pydantic']['version']} "
          f"(the server's is {own['server_pydantic']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
