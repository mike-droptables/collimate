# SPDX-License-Identifier: MIT
"""Fixtures for the LIVE agent-surface suite (agent-v2 §9.1.7).

Every test in this tree drives the REAL product path: this conftest boots a
real uvicorn on an ephemeral port over a throwaway data directory, seeds a
real channel through the public API, and drives the shipped SPA in a real
headless Chromium. Nothing is stubbed, so nothing here may be assumed —
each precondition is checked and, when missing, SKIPS with the reason named.

Isolation
---------
The server's data dir IS its process CWD (``main.cli_main``'s contract:
``.collimate/`` is CWD-relative). The suite therefore
spawns uvicorn with ``cwd=<pytest tmp dir>`` and never passes ``--data-dir``
— the developer's own ``collimatedev/.collimate`` is untouched. The bundled
generators and renderers (``content/`` beside ``api/``) and the built SPA
(``api/static/``) resolve from the api MODULE directory, so a foreign CWD
keeps them.

The port is ephemeral (never 5001): another Collimate server — a dev session,
another agent — may own the default port, and binding it would either fail or,
worse, leave the tests talking to somebody else's server.

Keyring
-------
The suite does NOT set ``PYTHON_KEYRING_BACKEND=…null.Keyring`` the way the
`run-app` skill does. Every account a scenario creates stores its secret
through the server's keyring, so the server needs one that STORES — the null
backend refuses each save with a 503 and would take every scenario down with
it. The server therefore ALWAYS runs on ``tests_live/_memory_keyring`` — a
dict for the life of the server process. Never the real OS keychain: the
secrets a session saves are the developer's own (their OpenRouter key, their
Claude login's access token), and in the real keychain they would outlive a
server this suite kills — teardown escalates to SIGKILL, a crashed pytest
never deletes its accounts — as orphan ``collimate_<id>_…`` slots under
account ids no registry names. Only the server process reads its keyring
(``keystore`` is imported by ``accounts`` alone; generators get the resolved
secrets as env), so a process-local store is complete. The OS keychain is
still READ, by this pytest process, in a bounded child (:func:`_keychain_key`)
for the OpenRouter key a developer keeps there; when it does not answer (a
D-Bus with no unlocked session), a gate whose key could only have come from
it skips, saying so.

Accounts and runners
--------------------
A run is a saved RUNNER: an id, a revision and the cell it rewrites. Which
model answers, over which harness, is the ``model`` account bound to that
runner — so every scenario here first creates an account through
``/api/accounts`` (a base credential plus a model entry carrying
``metadata.harness``), then posts a runner built from one of a generator's
shipped configs (the Agent cell's ``agent`` on its annotation, chat or
terminal surface, or ``web``) with that entry bound to its ``model`` role. The accounts go
into the server's own registry and are deleted when the session ends; the
runners are deleted when the test that made them ends.

Gates
-----
``COLLIMATE_LIVE_CLAUDE_CODE=1`` + a ``claude`` on PATH + either
                                  ``ANTHROPIC_API_KEY`` or a Claude
                                  subscription (``CLAUDE_CODE_OAUTH_TOKEN``, or
                                  the CLI's own login) — the claude-code
                                  scenarios.
``COLLIMATE_LIVE_DOCKER=1`` + an ``OPENROUTER_API_KEY`` (env OR keychain)
                                  the opencode/goose container scenarios.
``COLLIMATE_LIVE_PROVIDER=<p>``   + ``<P>_API_KEY`` (openrouter, cerebras or
                                  gemini; the OpenRouter key may come from the
                                  keychain) + ``COLLIMATE_LIVE_MODEL_<P>`` —
                                  the host pydantic-ai scenarios
                                  (``test_live_multi_runner.py``), no docker.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

# ── layout ────────────────────────────────────────────────────────────────
API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_INDEX = os.path.join(API_DIR, "static", "index.html")

# ── env gates (read once; the environment is fixed for a pytest session) ──
CLAUDE_GATE = os.environ.get("COLLIMATE_LIVE_CLAUDE_CODE") == "1"
DOCKER_GATE = os.environ.get("COLLIMATE_LIVE_DOCKER") == "1"

#: Cheap, deterministic models — a live suite that bills a flagship per turn
#: gets switched off. Override for a one-off investigation. Each is the
#: ``model`` of the account entry the scenarios bind, which is the only place
#: a run's model comes from.
CLAUDE_MODEL = os.environ.get("COLLIMATE_LIVE_CLAUDE_MODEL", "haiku")
OPENROUTER_MODEL = os.environ.get(
    "COLLIMATE_LIVE_OPENROUTER_MODEL", "openai/gpt-5-mini")

#: The provider-generic gate: ``COLLIMATE_LIVE_PROVIDER`` names the service
#: of an API-key account whose model entry runs on the host-locus
#: pydantic-ai harness (no container). The key is the provider's standard
#: env var; the model is ``COLLIMATE_LIVE_MODEL_<PROVIDER>`` (for openrouter
#: ``COLLIMATE_LIVE_OPENROUTER_MODEL`` is accepted too, the docker gate's
#: spelling).
PROVIDER_GATE = os.environ.get("COLLIMATE_LIVE_PROVIDER", "").strip().lower()
PROVIDER_KEY_ENV = {"openrouter": "OPENROUTER_API_KEY",
                    "cerebras": "CEREBRAS_API_KEY",
                    "gemini": "GEMINI_API_KEY"}
PROVIDER_MODEL = os.environ.get(f"COLLIMATE_LIVE_MODEL_{PROVIDER_GATE.upper()}") or (
    os.environ.get("COLLIMATE_LIVE_OPENROUTER_MODEL", "")
    if PROVIDER_GATE == "openrouter" else "")

#: A chat run's surface files, RELATIVE to the run's folder
#: ``_runs/<label>/`` (the POST answer and ``/runs/active`` carry ``folder``).
#: Resolve one with ``api.run_path(run_id, rel)``, or find it in a cell with
#: ``api.ui_path(cell_id, rel)``.
CHAT_POINTER = "chat/conversation.chat"
CHAT_TRANSCRIPT = "chat/main.json"
#: Run LOG artifacts — read with ``api.run_log(run, path)``, never from the
#: cell. Both are log-category writes, which reach the host at the run's next
#: flush: the terminal transcript therefore lands in the log when the run
#: ends, however often the tail appended to it during the TTY.
TERMINAL_TRANSCRIPT = "_agent/terminal/transcript.json"
SESSION_RECORD = "_agent/session.json"


# ══════════════════════════════════════════════════════════════════════════
# pytest wiring
# ══════════════════════════════════════════════════════════════════════════

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: talks to a real model / container / browser")


def pytest_collect_file(file_path, parent):
    """Collect ``test_live_*.py`` here.

    ``api/pyproject.toml`` pins ``python_files = ["test_pytest_*.py"]``
    (``tests/test_*.py`` are standalone scripts, not pytest modules), which
    would otherwise make this tree uncollectable even when named explicitly. Opting THIS directory in from its own conftest keeps
    the global rule — and the ``testpaths = ["tests"]`` exclusion that keeps
    a bare ``pytest`` away from here — exactly as they are.

    A file named on the command line is collected by pytest itself whatever
    ``python_files`` says, so the hook stands down for those: collecting it
    here as well would run every test twice.
    """
    if (file_path.suffix == ".py" and file_path.name.startswith("test_live_")
            and not parent.session.isinitpath(file_path)):
        return pytest.Module.from_parent(parent, path=file_path)
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Publish each phase's report on the item so fixtures can see, at
    teardown, whether the test failed — that is when evidence is worth
    collecting and when it is only noise."""
    outcome = yield
    setattr(item, "rep_" + outcome.get_result().when, outcome.get_result())


def _failed(request) -> bool:
    return any(getattr(request.node, "rep_" + w, None) is not None
               and getattr(request.node, "rep_" + w).failed
               for w in ("setup", "call"))


# ══════════════════════════════════════════════════════════════════════════
# Environment probes
# ══════════════════════════════════════════════════════════════════════════

def _probe(code: str, timeout: float = 15.0) -> bool:
    """Run ``code`` in a child interpreter; True iff it exits 0 in time.

    A child, not an import, because the failure mode we are probing FOR is a
    hang (keyring blocking on a D-Bus that will never answer) — which an
    in-process call could not survive.
    """
    try:
        return subprocess.run(
            [sys.executable, "-c", code], timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.fixture(scope="session")
def keychain_ok() -> bool:
    """Whether the OS keychain answers at all (see the module docstring)."""
    return _probe("import keyring; keyring.get_password('collimate', 'x')")


@pytest.fixture(scope="session")
def evidence(tmp_path_factory):
    """Failure evidence lands under ``--basetemp`` so a CI run's artifacts
    and a local ``pytest --basetemp=…`` both collect it in one place."""
    root = tmp_path_factory.getbasetemp() / "evidence"
    root.mkdir(parents=True, exist_ok=True)

    class Evidence:
        dir = root

        def path(self, name: str) -> str:
            return str(root / name)

        def text(self, name: str, body) -> str:
            p = root / name
            p.write_text(body if isinstance(body, str)
                         else json.dumps(body, indent=1), encoding="utf-8")
            return str(p)

        def shot(self, page, name: str) -> str:
            p = self.path(f"{name}.png")
            try:
                page.screenshot(path=p)
            except Exception:  # noqa: BLE001 — evidence must never mask a failure
                return ""
            return p

    return Evidence()


# ══════════════════════════════════════════════════════════════════════════
# The server
# ══════════════════════════════════════════════════════════════════════════

def _free_port() -> int:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    def __init__(self, base_url: str, proc, data_dir: str, log_path: str):
        self.base_url = base_url
        self.proc = proc
        self.data_dir = data_dir
        self.log_path = log_path

    def log_tail(self, lines: int = 120) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:])
        except OSError:
            return "(no server log)"


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """A uvicorn of our own: ephemeral port, throwaway CWD, real everything
    else. Torn down with SIGINT so the lifespan's ``stop_all_runs`` sweep
    runs and no generator subprocess or container is orphaned."""
    if not os.path.isfile(STATIC_INDEX):
        pytest.skip(
            "the built SPA is missing (api/static/index.html) — these tests "
            "drive the product path in a browser. Build it first: "
            "cd frontend && npm run build && cp -r dist ../api/static")
    if shutil.which("docker") is None and DOCKER_GATE:
        pytest.skip("COLLIMATE_LIVE_DOCKER=1 but no `docker` on PATH")

    data_dir = tmp_path_factory.mktemp("collimate-data")
    port = _free_port()
    env = dict(
        os.environ,
        PYTHONPATH=API_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""),
        PYTHONUNBUFFERED="1",
        # A server owns the containers it stamped with an id derived from its
        # data dir (api/instance.py), and OUR data dir is a pytest tmp dir
        # that exists for one session only. Every session therefore stamps
        # containers with an id no future server can recognise as its own, so
        # anything this suite strands — the teardown below escalates to
        # SIGKILL, which skips the lifespan sweep entirely — would sit on the
        # host forever. Opting back in to the pre-label global sweep makes the
        # NEXT session's boot clean up after the last one. Safe here because
        # the suite already documents that it must not run beside another
        # Collimate with a live container-backed run.
        COLLIMATE_REAP_ALL_INSTANCES="1",
        # The accounts registry defaults to the developer's own
        # ~/.config/collimate/accounts.json. The Key manager scenarios create
        # and delete accounts, so they get a registry of their own under the
        # throwaway data dir.
        COLLIMATE_ACCOUNTS_CONFIG=str(data_dir / "accounts.json"),
        # ...and a keyring of its own, in the server's memory (see the module
        # docstring): the developer's keys and Claude token never land in the
        # real OS keychain, so a SIGKILLed server cannot orphan them there.
        # It STORES — a backend that stores nothing would 503 every account
        # save and take the Key manager scenarios down with it.
        PYTHON_KEYRING_BACKEND="tests_live._memory_keyring.MemoryKeyring",
    )
    log_path = str(data_dir / "uvicorn.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(data_dir), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    srv = LiveServer(base, proc, str(data_dir), log_path)

    deadline = time.time() + 90
    while True:
        if proc.poll() is not None:
            log.flush()
            pytest.fail(f"uvicorn died (rc={proc.returncode}):\n{srv.log_tail()}")
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                json.loads(r.read())
                break
        except Exception:  # noqa: BLE001 — still booting
            if time.time() > deadline:
                log.flush()
                pytest.fail(f"uvicorn never answered /health:\n{srv.log_tail()}")
            time.sleep(0.5)

    yield srv

    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    finally:
        log.close()


@pytest.fixture(scope="session")
def app(server) -> str:
    """The base URL — SPA and API are the same origin (renderer modules are
    fetched from it, which is why a vite dev server cannot host these)."""
    return server.base_url


# ══════════════════════════════════════════════════════════════════════════
# The HTTP client + channel
# ══════════════════════════════════════════════════════════════════════════

class ApiError(RuntimeError):
    def __init__(self, method, path, code, body):
        super().__init__(f"{method} {path} -> {code}: {body[:600]}")
        self.code = code
        self.body = body


class LiveApi:
    """The product's own HTTP surface — every helper here is a call the app
    itself makes, so a test that uses them is still on the product path."""

    def __init__(self, base_url: str, channel: str):
        self.base = base_url
        self.channel = channel
        self.chq = urllib.parse.quote(channel)
        self.started_runs: list[str] = []
        #: run id -> its run folder ``_runs/<label>``, recorded when the run
        #: is seen (its POST answer, ``/runs/active``) so it outlives the run.
        self.folders: dict[str, str] = {}

    # ── plumbing ────────────────────────────────────────────────────────
    def request(self, method: str, path: str, body=None, timeout: float = 60,
                raw: bytes | None = None, content_type: str | None = None):
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        headers = {}
        if data is not None:
            headers["Content-Type"] = content_type or "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            raise ApiError(method, path, e.code,
                           e.read().decode("utf-8", "replace")) from None
        try:
            return json.loads(payload)
        except ValueError:
            return payload.decode("utf-8", "replace")

    # ── cells ───────────────────────────────────────────────────────────
    def new_cell(self, name: str, files=None) -> str:
        """A fresh standard cell. Each scenario gets its own so a stuck
        session can never leak into the next test."""
        files = files or [{"name": "notes.md",
                           "content": "# Live suite notes\n\nA small file.\n"}]
        refs = [{"path": f["name"],
                 "blob_hash": self.request("POST", "/api/blob/stream", raw=f["content"].encode(),
                                           content_type="application/octet-stream")["blob_hash"]}
                for f in files]
        d = self.request("POST", f"/api/channels/{self.chq}/import", {"stacks": [
            {"name": "", "cells": [{"name": name, "files": refs, "dirs": []}]}]})
        return d["cell_ids"][0]

    def cell_files(self, cell_id: str) -> list[dict]:
        return self.request(
            "GET", f"/api/channels/{self.chq}/cells/{cell_id}/files")["files"]

    def cell_paths(self, cell_id: str) -> set[str]:
        return {f["path"] for f in self.cell_files(cell_id) if not f["deleted"]}

    def cell_file(self, cell_id: str, path: str):
        return self.request(
            "GET",
            f"/api/channels/{self.chq}/cells/{cell_id}/files/"
            f"{urllib.parse.quote(path)}")

    def surface(self, cell_id: str, path: str):
        """Live registration metadata for a physical mounted file, or None."""
        try:
            return self.request("GET", f"/api/channels/{self.chq}/cells/{cell_id}/surfaces/"
                                f"{urllib.parse.quote(path, safe='/')}")
        except ApiError as exc:
            if exc.code == 404:
                return None
            raise

    def write_file(self, cell_id: str, path: str, text: str):
        """Submit a conditional text mutation against the observed revision."""
        current = next((row for row in self.cell_files(cell_id) if row['path'] == path), None)
        operation = {
            'operation_id': str(uuid.uuid4()),
            'mutations': [{
                'path': path, 'writer_id': f'live-test:{uuid.uuid4()}', 'edit_seq': 1,
                'base_revision': current['file_revision'] if current else 0,
                'kind': 'put', 'content_text': text,
                'language': current['language'] if current else 'text',
            }],
            'resolves': [],
        }
        receipt = self.request('POST', f'/api/channels/{self.chq}/cells/{cell_id}/files/mutate', operation)
        assert receipt['operation_id'] == operation['operation_id']
        assert receipt['outcome'] == 'accepted', receipt
        return receipt

    # ── runs ────────────────────────────────────────────────────────────
    def start_runner(self, cell_id: str, runner: dict) -> str:
        """Run a saved runner against one cell, the way the runners panel does.

        A run names a runner and the revision it was read at — nothing else
        is negotiable at the call site: the prompt, the settings and the
        bound accounts are the runner's own.
        """
        body = {"runner": runner["id"], "runner_revision": runner["revision"],
                "cell_id": cell_id}
        answer = self.request("POST", f"/api/channels/{self.chq}/runs", body)
        run_id = answer["run_id"]
        self.folders[run_id] = answer["folder"]
        self.started_runs.append(run_id)
        return run_id

    def run_folder(self, run_id: str) -> str:
        """The run's folder ``_runs/<label>`` — recorded when the run was
        started or seen live, else looked up in ``/runs/active``."""
        if run_id not in self.folders:
            for info in self.request("GET", f"/api/channels/{self.chq}/runs/active"):
                self.folders[info["run_id"]] = info["folder"]
        return self.folders[run_id]

    def run_path(self, run_id: str, rel: str) -> str:
        """The cell path of ``rel`` inside the run's folder."""
        return f"{self.run_folder(run_id)}/{rel}"

    def ui_path(self, cell_id: str, rel: str) -> str | None:
        """The cell path of a run's ``rel`` UI file in ``cell_id`` — the one
        ``_runs/<label>/<rel>`` there, whichever run owns it — or None."""
        return next((p for p in sorted(self.cell_paths(cell_id))
                     if p.startswith("_runs/") and p.endswith("/" + rel)), None)

    def active_run_on(self, cell_id: str, *, besides: str = "") -> str | None:
        """The live run on ``cell_id`` other than ``besides``, or None — how
        a scenario shows that nothing started on the cell by itself: a
        conclusion ends its run, and the next run is the user's to start. A
        run found here is tracked for teardown like one the scenario started."""
        for info in self.request("GET", f"/api/channels/{self.chq}/runs/active"):
            if info["cell_id"] == cell_id and info["run_id"] != besides:
                self.folders[info["run_id"]] = info["folder"]
                if info["run_id"] not in self.started_runs:
                    self.started_runs.append(info["run_id"])
                return info["run_id"]
        return None

    def active_runs(self) -> list[dict]:
        """``/runs/active`` — every live run of the channel, folders recorded."""
        rows = self.request("GET", f"/api/channels/{self.chq}/runs/active")
        for info in rows:
            self.folders[info["run_id"]] = info["folder"]
        return rows

    def runs_on(self, cell_id: str) -> list[dict]:
        return [r for r in self.active_runs() if r["cell_id"] == cell_id]

    def track(self, run_id: str) -> str:
        """Own a run the HOST started (a clone's run) for teardown."""
        if run_id not in self.started_runs:
            self.started_runs.append(run_id)
        return run_id

    def stop_run(self, run_id: str):
        return self.request("DELETE", f"/api/channels/{self.chq}/runs/{run_id}")

    # ── the canvas ──────────────────────────────────────────────────────
    def main_stacks(self) -> list[dict]:
        """This channel's main-canvas stacks, in canvas order, from
        ``/api/state`` (what the SPA renders)."""
        state = self.request("GET", "/api/state")
        for ch in state["channels"]:
            if ch["id"] == self.channel:
                return ch["main_stacks"]
        raise AssertionError(f"channel {self.channel!r} is not in /api/state")

    def stack_index_of(self, cell_id: str) -> int | None:
        for i, stack in enumerate(self.main_stacks()):
            if any(c["id"] == cell_id for c in stack["cells"]):
                return i
        return None

    def state_files(self, cell_id: str) -> set[str]:
        """The cell's file names as the canvas holds them (in-memory tip
        plus the user's pending drafts) — ``cell_paths`` is the committed
        tip only."""
        for stack in self.main_stacks():
            for cell in stack["cells"]:
                if cell["id"] == cell_id:
                    return {f["name"] for f in cell["files"]}
        raise AssertionError(f"cell {cell_id} is not on the canvas")

    def stop_started_runs(self) -> None:
        """Stop whatever this client started and is still live. Best effort:
        teardown never masks the verdict."""
        for run_id in reversed(self.started_runs):
            try:
                if self.run_state(run_id).get("is_active"):
                    self.stop_run(run_id)
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

    def run_events(self, run_id: str, since: int = 0):
        return self.request(
            "GET",
            f"/api/channels/{self.chq}/runs/{run_id}/events_since"
            f"?since_id={since}")

    def run_state(self, run_id: str) -> dict:
        """Phase/status only — ``since_id`` past every row so a long run's
        event history is not re-shipped on every poll."""
        return self.run_events(run_id, since=2 ** 62)

    def run_artifacts(self, run_id: str) -> dict:
        return self.request(
            "GET", f"/api/channels/{self.chq}/runs/{run_id}/artifacts")

    def run_log(self, run_id: str, path: str):
        """A run LOG artifact's content (parsed when it is JSON), or None
        while the run has not logged ``path``. What a run keeps for itself —
        a terminal run's transcript, the ACP session record, the surface
        files its run end clears — lives in its log, never in the cell."""
        for art in self.run_artifacts(run_id)["artifacts"]:
            if art["category"] == "log" and art["path"] == path:
                return self.request("GET", f"/api/blob/{art['blob_hash']}")
        return None

    def run_log_paths(self, run_id: str) -> set[str]:
        return {a["path"] for a in self.run_artifacts(run_id)["artifacts"]
                if a["category"] == "log"}

    # ── waiting ─────────────────────────────────────────────────────────
    def run_died(self, run_id: str) -> dict | None:
        """The run's terminal state, or None while it is alive.

        Every wait that expects a LIVE session passes its run id: a crashed
        generator would otherwise burn the whole timeout and then report the
        symptom ("no reply") instead of the cause ("run failed: OSError …"),
        which is the difference between a five-second diagnosis and a
        five-minute one.
        """
        state = self.run_state(run_id)
        if state.get("is_terminal") or not state.get("is_active"):
            if state.get("status") not in (None, "running"):
                return state
        return None

    def wait_until(self, pred, timeout: float, label: str, every: float = 1.0,
                   run_id: str | None = None):
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            try:
                v = pred()
                if v:
                    return v
            except Exception as exc:  # noqa: BLE001 — a poll may race a write
                last = exc
            if run_id:
                dead = self.run_died(run_id)
                if dead:
                    raise AssertionError(
                        f"the run ended while waiting for {label} — "
                        f"status={dead.get('status')!r} "
                        f"reason={dead.get('failure_reason')!r}")
            time.sleep(every)
        raise AssertionError(
            f"timed out after {timeout:.0f}s waiting for {label}"
            + (f" (last poll error: {last!r})" if last else ""))

    def happens_within(self, pred, timeout: float, every: float = 2.0,
                       run_id: str | None = None) -> bool:
        """Whether ``pred`` goes true inside ``timeout`` — for the assertions
        that have to say something did NOT happen (a contract that has not
        landed) as confidently as that it did. A dead run still raises: "it
        never happened" would be a lie when the session crashed."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if pred():
                    return True
            except Exception:  # noqa: BLE001 — a poll may race a write
                pass
            if run_id:
                dead = self.run_died(run_id)
                if dead:
                    raise AssertionError(
                        f"the run ended mid-wait — status={dead.get('status')!r} "
                        f"reason={dead.get('failure_reason')!r}")
            time.sleep(every)
        return False

    def wait_idle(self, run_id: str, timeout: float = 180):
        """``awaiting_input`` — the session finished a turn and is listening."""
        return self.wait_until(
            lambda: self.run_state(run_id).get("phase") == "awaiting_input",
            timeout, f"run {run_id[:8]} to go idle")

    def wait_busy(self, run_id: str, timeout: float = 60):
        return self.wait_until(
            lambda: self.run_state(run_id).get("phase") != "awaiting_input",
            timeout, f"run {run_id[:8]} to start a turn", every=0.4)

    def wait_finished(self, run_id: str, timeout: float = 300):
        return self.wait_until(
            lambda: not self.run_state(run_id).get("is_active"),
            timeout, f"run {run_id[:8]} to finish")

    def wait_file(self, cell_id: str, path: str, timeout: float = 120,
                  run_id: str | None = None):
        return self.wait_until(
            lambda: path in self.cell_paths(cell_id), timeout,
            f"{path} to appear in the cell", run_id=run_id)

    # ── the chat transcript ─────────────────────────────────────────────
    def transcript(self, cell_id: str) -> dict:
        path = self.ui_path(cell_id, CHAT_TRANSCRIPT)
        if path is None:
            raise ApiError("GET", f"<{CHAT_TRANSCRIPT} in {cell_id}>", 404, "")
        return self.cell_file(cell_id, path)

    def messages(self, cell_id: str) -> list[dict]:
        try:
            return self.transcript(cell_id).get("messages", [])
        except ApiError:
            return []

    def n_assistant(self, cell_id: str) -> int:
        return sum(1 for m in self.messages(cell_id)
                   if m.get("role") == "assistant" and (m.get("content") or "").strip())

    def wait_assistant(self, cell_id: str, n: int, timeout: float = 240,
                       run_id: str | None = None):
        """Wait until the transcript holds ``n`` non-empty assistant replies.

        Turn counting, not phase watching: a programmatic turn (one started
        by the runner, not the composer) can flip the phase without the
        composer ever seeing a reply, so "my message was answered" has to be
        counted on the transcript.
        """
        self.wait_until(lambda: self.n_assistant(cell_id) >= n, timeout,
                        f"assistant reply #{n}", run_id=run_id)
        time.sleep(1.5)          # let the final flush land before reading
        return self.messages(cell_id)


# ── the annotation context ─────────────────────────────────────────────


def annotation_context(api, cell_id: str, context_id: str) -> dict:
    """One annotation context of the cell (a run's id, or ``cell``)."""
    return api.request("GET", f"/api/channels/{api.chq}/cells/{cell_id}/annotations/"
                       f"{urllib.parse.quote(context_id)}")


def post_annotation(api, cell_id: str, context_id: str, text: str, *, mode: str = "queued") -> str:
    """Post one user message into a context through the product's route, as
    the status pane's composer does, and return its id."""
    message = api.request("POST", f"/api/channels/{api.chq}/cells/{cell_id}/annotations", {
        "operation_id": str(uuid.uuid4()), "context_id": context_id, "body": text, "mode": mode})
    return message["id"]


def wait_reply(api, cell_id: str, run_id: str, root_id: str, timeout: float = 300) -> dict:
    """Wait until run ``run_id`` has read the message ``root_id`` and posted
    a reply in its thread; returns the reply."""
    def reply():
        messages = annotation_context(api, cell_id, run_id)["messages"]
        root = next((m for m in messages if m["id"] == root_id), None)
        if root is None or not root["read_at"]:
            return None
        return next((m for m in messages if m["parent_id"] == root_id and m["author"] == "run"), None)
    return api.wait_until(reply, timeout, f"run {run_id[:8]}'s reply to {root_id}", run_id=run_id)


@pytest.fixture(scope="session")
def channel(app) -> str:
    """A channel created from the bundled Standard suggestion (the shipped
    generators and renderers, no tutorial): its immutable UUID, independent
    of its label."""
    def post(method, path, body):
        request = urllib.request.Request(app + path, method=method, data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)

    name = "Live Agent Surfaces"
    return post("POST", "/api/channels?name=" + urllib.parse.quote(name),
                {"suggested": {"id": "standard", "tutorial": False}})["id"]


@pytest.fixture
def api(app, channel, server, request, evidence):
    """A per-test client that stops whatever runs the test started.

    Function-scoped so a scenario can never leave a live agent session
    holding its cell (and its container) for the next one.
    """
    client = LiveApi(app, channel)
    yield client
    if _failed(request):
        # The run's own record BEFORE it is stopped: its event log (the
        # generator's log lines, phases, refusals) and its artifacts are
        # what a red scenario is diagnosed from — the server log holds
        # only HTTP lines.
        for run_id in client.started_runs:
            for what, read in (("events", client.run_events),
                               ("artifacts", client.run_artifacts)):
                try:
                    evidence.text(f"{request.node.name}.{run_id[:8]}.{what}.json",
                                  read(run_id))
                except Exception:  # noqa: BLE001 — evidence never masks the verdict
                    pass
    client.stop_started_runs()
    if _failed(request):
        evidence.text(f"{request.node.name}.server.log", server.log_tail(400))


# ══════════════════════════════════════════════════════════════════════════
# Accounts and runners — what a run is made of
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def session_api(app, channel, server) -> LiveApi:
    """A client for the fixtures that outlive a single test — the accounts,
    which are created once and deleted when the session ends."""
    return LiveApi(app, channel)


def credential_account(api: LiveApi, name: str, service: str, key: str) -> dict:
    """A base API-key account for ``service``, created through the public
    accounts API. The key reaches the test server only: it is stored in that
    server's own registry (``COLLIMATE_ACCOUNTS_CONFIG``) and its keyring
    entry goes with the account when it is deleted."""
    return api.request("POST", "/api/accounts", {
        "name": name, "kind": "account", "service": service,
        "auth_type": "api_key", "secrets": {"api_key": key},
    })


def model_entry(api: LiveApi, owner: dict, name: str, model: str,
                harness: str) -> dict:
    """A model entry under ``owner``: the model id AND the harness that runs
    it. The agent's ``backend`` reads ``metadata.harness`` off the bound
    entry, so this pair is what decides which harness a runner starts."""
    entry = api.request("POST", "/api/accounts", {
        "name": name, "kind": "model", "credential_account_id": owner["id"],
        "model": model, "metadata": {"harness": harness},
    })
    assert not entry.get("model_configuration_error"), entry
    return entry


def delete_accounts(api: LiveApi, *identities: str) -> None:
    for identity in identities:
        try:
            api.request("DELETE", f"/api/accounts/{urllib.parse.quote(identity)}")
        except Exception:  # noqa: BLE001 — cleanup never masks the verdict
            pass


def make_runner(api: LiveApi, config_id: str, model_id: str | None, name: str,
                *, generator: str, settings: dict | None = None,
                permissions: dict | None = None, packages: dict | None = None) -> dict:
    """A saved runner from one of ``generator``'s shipped configs, with
    ``model_id`` bound to its ``model`` role (the Agent cell's ``agent``;
    ``web`` binds no model, so ``None``).

    Every setting is the shipped config's own, so a scenario tests what the
    product ships rather than a hand-built definition; ``settings`` (main
    config values, such as the agent's ``surface``), ``permissions`` and
    ``packages`` are merged over it where a scenario needs another surface.
    """
    template = api.request(
        "GET", f"/api/runner_generators/{generator}/configs/{config_id}")
    runner = {**template["runner"], "id": f"live-{uuid.uuid4().hex[:8]}",
              "name": name, "generator": generator,
              "accounts": {"model": model_id} if model_id else {}}
    if settings:
        [main] = runner["settings"]
        runner["settings"] = {main: {**runner["settings"][main], **settings}}
    if permissions:
        runner["permissions"] = {**runner.get("permissions", {}), **permissions}
    if packages:
        runner["packages"] = {**runner.get("packages", {}), **packages}
    api.request("POST", "/api/runners", {"runner": runner, "files": template["files"]})
    entry = next(r for r in api.request("GET", "/api/runners")
                 if r["id"] == runner["id"])
    assert not entry.get("invalid"), entry
    return entry


def surface_overrides(config_id: str, surface: str | None) -> dict:
    """The agent runner overrides that put a harness preset on ``surface``
    (``annotation``, ``chat`` or ``terminal``): the terminal surface runs a
    container harness's TTY in its container (docker: full); Claude Code's
    TTY is this machine's claude (docker: none), which asks before its
    commands (a CLI on this machine refuses commands: deny)."""
    if surface is None:
        return {}
    out: dict = {"settings": {"surface": surface}}
    if surface == "terminal":
        out["permissions"] = {"docker": "full"}
        if config_id == "claude-code":
            out["permissions"] = {"docker": "none", "commands": "ask"}
    return out


@pytest.fixture
def runners(api):
    """``runners(config_id, model, name, surface=…)`` → a saved runner entry,
    deleted when the test ends so the next one's catalog is clean.
    ``generator`` (default ``agent``; ``web``) picks whose shipped config,
    ``config_id`` is one of that generator's own (``default``,
    ``claude-code``, ``opencode``, …) and ``surface`` the agent's Surface
    setting (``annotation``, ``chat`` or ``terminal``; the preset's own when
    omitted); ``model`` is None for a generator that binds no model (web)."""
    made: list[str] = []

    def make(config_id: str, model: dict | None, name: str, *,
             generator: str = "agent", surface: str | None = None) -> dict:
        entry = make_runner(api, config_id, model["id"] if model else None,
                            f"{name} {uuid.uuid4().hex[:6]}",
                            generator=generator, **surface_overrides(config_id, surface))
        made.append(entry["id"])
        return entry

    yield make
    # This fixture finalizes BEFORE ``api``, so a run the test left alive
    # would still be holding a runner we are about to delete — stop them here
    # rather than deleting a definition out from under a live run.
    api.stop_started_runs()
    for runner_id in made:
        try:
            api.request("DELETE", f"/api/runners/{urllib.parse.quote(runner_id)}")
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


# ══════════════════════════════════════════════════════════════════════════
# Gates — each SKIPS with the missing thing named, never passes vacuously
# ══════════════════════════════════════════════════════════════════════════

#: The ``claude`` CLI's own login on this machine — where ``claude /login``
#: keeps its OAuth credentials (``$CLAUDE_CONFIG_DIR`` relocates it).
def claude_login_path() -> str:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, ".credentials.json")


def _claude_login_token(path: str) -> tuple[str, str]:
    """``(token, "")`` from the machine's Claude login, or ``("", why)``.

    Only the ACCESS token is taken — never the refresh token. The product
    refuses to share the credential file itself (the SDK's
    ``generators/_agent_shared/harnesses/claude_code/backend.py``:
    ``_prepare_host`` — the CLI rotates single-use refresh tokens and
    replaces the file by rename, so a shared copy logs the user out of their
    own CLI; ``claude_subscription_token`` only reads the account's token); an access token handed over as
    ``CLAUDE_CODE_OAUTH_TOKEN`` is used and never refreshed, so the machine's
    login is untouched. It is short-lived (hours), unlike the year-long
    ``claude setup-token`` token the Key manager asks a user to paste — a
    test convenience, disclosed in the README; an expiring one skips. The
    value stays in this
    process and the test server's account: it is never written to a file,
    logged, or put in an assertion message — a failure names the PATH.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            oauth = (json.load(fh) or {}).get("claudeAiOauth") or {}
    except FileNotFoundError:
        return "", f"there is no Claude login at {path} (run `claude` and /login)"
    except (OSError, ValueError):
        return "", f"the Claude login at {path} could not be read as JSON"
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return "", f"the Claude login at {path} holds no OAuth access token"
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time() + 30 * 60:
        return "", (f"the access token in {path} expires within 30 minutes — "
                    "run any `claude` command so the CLI refreshes it, then "
                    "rerun (or export CLAUDE_CODE_OAUTH_TOKEN from `claude "
                    "setup-token`, which lasts a year and is what the Key "
                    "manager asks a user to paste)")
    return token, ""


def _claude_subscription_token() -> tuple[str, str]:
    """The Claude subscription token this machine can bill, or ``("", why)``.

    ``CLAUDE_CODE_OAUTH_TOKEN`` first — what ``claude setup-token`` prints and
    the Key manager's Access token field takes — then the CLI's own login.
    """
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env, ""
    return _claude_login_token(claude_login_path())


def subscription_account(api: LiveApi, name: str, token: str) -> dict:
    """A Claude SUBSCRIPTION account, the shape the Key manager's Anthropic →
    Subscription form saves: the token as the ``access_token`` secret, the
    plan and the app it belongs to in ``metadata``. Verified the product way
    (``/api/accounts/check`` sends one bearer request) before a run leans on
    it, so an expired or refused token fails HERE, not as a hung session."""
    record = {"name": name, "kind": "account", "service": "anthropic",
              "auth_type": "subscription", "secrets": {"access_token": token},
              "metadata": {"harness": "claude-code", "plan": "claude_code"}}
    try:
        check = api.request("POST", "/api/accounts/check", record, timeout=90)
    except ApiError as exc:
        check = {"valid": False, "message": f"HTTP {exc.code}"}
    if not check.get("valid"):
        pytest.skip("the Claude subscription token was refused by Verify: "
                    f"{str(check.get('message'))[:200]}")
    return api.request("POST", "/api/accounts", record)


@pytest.fixture(scope="session")
def claude_code(session_api):
    """The model entry the claude-code scenarios run on.

    The harness is the local ``claude`` CLI driven over ACP. It bills either
    an Anthropic API key (``ANTHROPIC_API_KEY``: an ``api_key`` account) or,
    with no key set, the machine's Claude SUBSCRIPTION: a ``subscription``
    account made from ``CLAUDE_CODE_OAUTH_TOKEN`` or the ``claude`` CLI's own
    login (:func:`claude_login_path`). Either way a ``claude-code`` model entry
    goes under it. Each missing precondition is named.
    """
    if not CLAUDE_GATE:
        pytest.skip("set COLLIMATE_LIVE_CLAUDE_CODE=1 to run the claude-code "
                    "scenarios (they bill an Anthropic key or this machine's "
                    "Claude subscription)")
    if shutil.which("claude") is None:
        pytest.skip("no `claude` CLI on PATH — the claude-code harness drives "
                    "it over ACP")
    tag = uuid.uuid4().hex[:6]
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        owner = credential_account(session_api, f"Anthropic live {tag}",
                                   "anthropic", key)
    else:
        token, why = _claude_subscription_token()
        if not token:
            pytest.skip("no ANTHROPIC_API_KEY and no Claude subscription "
                        f"token: {why}")
        owner = subscription_account(session_api, f"Claude plan live {tag}", token)
        del token
    entry = model_entry(session_api, owner, f"Claude Code live {tag}",
                        CLAUDE_MODEL, "claude-code")
    yield entry
    delete_accounts(session_api, entry["id"], owner["id"])


def _keychain_key(name: str) -> str:
    """One provider key out of the OS keychain, read in a bounded child.

    A child, not an import, for the reason :func:`_probe` gives: the failure
    being guarded against is a D-Bus wait that never returns.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-c",
             "import keyring, sys; sys.stdout.write("
             "keyring.get_password('collimate', sys.argv[1]) or '')", name],
            timeout=15, capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return done.stdout.strip()


@pytest.fixture(scope="session")
def openrouter_key(keychain_ok) -> str:
    """The key the container scenarios bill to, or ``""``.

    Read HERE and handed to the test server as an account secret — the
    keychain is this machine's, not the server's. An env var is accepted
    first, since that is what CI has.
    """
    return os.environ.get("OPENROUTER_API_KEY") or (
        _keychain_key("OPENROUTER_API_KEY") if keychain_ok else "")


@pytest.fixture(scope="session")
def _openrouter_models(session_api, openrouter_key):
    """``(harness) -> model entry`` under one OpenRouter account.

    One account for the session, one model entry per harness (opencode and
    goose differ only in ``metadata.harness``), all deleted at the end.
    """
    tag = uuid.uuid4().hex[:6]
    owner = credential_account(session_api, f"OpenRouter live {tag}",
                               "openrouter", openrouter_key)
    entries: dict[str, dict] = {}

    def entry_for(harness: str) -> dict:
        if harness not in entries:
            entries[harness] = model_entry(
                session_api, owner, f"{harness} live {tag}",
                OPENROUTER_MODEL, harness)
        return entries[harness]

    yield entry_for
    delete_accounts(session_api, *(e["id"] for e in entries.values()), owner["id"])


@pytest.fixture
def container_agent(openrouter_key, keychain_ok, request):
    """The gate for the opencode/goose scenarios: opt-in plus a real key.

    Returns the ``(harness) -> model entry`` factory. The account fixture is
    pulled only AFTER the gate passes — a skipped scenario must not create an
    account it will never use.
    """
    if not DOCKER_GATE:
        pytest.skip("set COLLIMATE_LIVE_DOCKER=1 to run the containerized "
                    "harness scenarios (may build images on first run)")
    if not openrouter_key:
        pytest.skip(
            "no OPENROUTER_API_KEY — not in the environment and "
            + ("not in the OS keychain under the `collimate` service"
               if keychain_ok else
               "the OS keychain did not answer, so it could not be read"))
    return request.getfixturevalue("_openrouter_models")


@pytest.fixture(scope="session")
def _provider_model(session_api, openrouter_key):
    """One API-key account for ``COLLIMATE_LIVE_PROVIDER`` and one model
    entry on the host-locus ``pydantic-ai`` harness, deleted at the end.
    Pulled by :func:`provider_model` only once its gate has passed."""
    key = os.environ.get(PROVIDER_KEY_ENV[PROVIDER_GATE]) or (
        openrouter_key if PROVIDER_GATE == "openrouter" else "")
    tag = uuid.uuid4().hex[:6]
    owner = credential_account(session_api, f"{PROVIDER_GATE} live {tag}",
                               PROVIDER_GATE, key)
    entry = model_entry(session_api, owner, f"{PROVIDER_GATE} pydantic-ai live {tag}",
                        PROVIDER_MODEL, "pydantic-ai")
    yield entry
    delete_accounts(session_api, entry["id"], owner["id"])


@pytest.fixture
def provider_model(openrouter_key, keychain_ok, request) -> dict:
    """The gate for the provider-generic scenarios: the model entry of
    ``COLLIMATE_LIVE_PROVIDER``'s account, on pydantic-ai (no container).
    Each missing precondition is named."""
    if not PROVIDER_GATE:
        pytest.skip("set COLLIMATE_LIVE_PROVIDER=openrouter|cerebras|gemini (plus "
                    "its API key and COLLIMATE_LIVE_MODEL_<PROVIDER>) to run "
                    "the pydantic-ai scenarios")
    if PROVIDER_GATE not in PROVIDER_KEY_ENV:
        pytest.skip(f"COLLIMATE_LIVE_PROVIDER={PROVIDER_GATE!r} is not one of "
                    f"{sorted(PROVIDER_KEY_ENV)}")
    key_env = PROVIDER_KEY_ENV[PROVIDER_GATE]
    if not os.environ.get(key_env) and not (PROVIDER_GATE == "openrouter" and openrouter_key):
        pytest.skip(f"no {key_env} for COLLIMATE_LIVE_PROVIDER={PROVIDER_GATE}"
                    + (" (not in the environment or the OS keychain)"
                       if PROVIDER_GATE == "openrouter" and keychain_ok else ""))
    if not PROVIDER_MODEL:
        pytest.skip(f"set COLLIMATE_LIVE_MODEL_{PROVIDER_GATE.upper()} to the model "
                    f"the {PROVIDER_GATE} scenarios run on")
    return request.getfixturevalue("_provider_model")


# ══════════════════════════════════════════════════════════════════════════
# The browser
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("python-playwright is not installed in this interpreter — "
                    "run under `uv run --with playwright python -m "
                    "pytest tests_live`, or install it once with "
                    "`uv pip install --python ../.venv/bin/python playwright`; then "
                    "`python -m playwright install chromium`")
    pw = sync_playwright().start()
    try:
        chromium = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium would not launch ({exc}) — run "
                    "`playwright install chromium`")
    yield chromium
    chromium.close()
    pw.stop()


@pytest.fixture
def page(browser, app, request, evidence):
    """One page per test.

    ONE page at a time, deliberately: when two views hold the same chat the
    second gets the single-attach 4409 ``busy`` state, and a scenario that
    tripped it would be measuring the wrong thing.
    """
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    # A default on every locator action and wait_for_function: without one, a
    # renderer that never mounts turns a scenario into a 40-minute hang
    # instead of a failure with a screenshot.
    ctx.set_default_timeout(45000)
    pg = ctx.new_page()
    logs: list[str] = []
    pg.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
    pg.goto(app + "/", wait_until="domcontentloaded")
    # Never ``networkidle``: the per-channel SSE stream never closes, so it
    # never fires. Wait for the shell to have painted instead — a freshly
    # seeded channel's MAIN canvas is empty (the seed lands in the nav
    # stacks), so waiting on a cell would time out here rather than in the
    # test that actually wants one.
    pg.wait_for_function(
        "() => (document.body.innerText || '').trim().length > 0",
        timeout=30000)
    time.sleep(2)
    yield pg
    if _failed(request):
        evidence.shot(pg, f"{request.node.name}.final")
        evidence.text(f"{request.node.name}.console.log", "\n".join(logs[-200:]))
    ctx.close()


class CellView:
    """The product-path handle on one cell: the canvas, the run light and
    the status caret in the cell head, the status pane (the cell's runs, a
    run's drilldown with its messages, All), the runners panel, and the
    surface renderers inside their shadow roots."""

    #: The chat renderer's host — identified by a control only IT has, so a
    #: sibling shadow host (a terminal) can never be mistaken for it.
    _CHAT_HOST = ("[...document.querySelectorAll(\"[data-cell-id='%s'] *\")]"
                  ".find(e => e.shadowRoot && e.shadowRoot.querySelector("
                  "'button[title=\\\"Send message\\\"]'))")

    def __init__(self, page, cell_id: str, evidence, name: str):
        self.page = page
        self.cell_id = cell_id
        self.ev = evidence
        self.name = name
        self.host = self._CHAT_HOST % cell_id
        self.sel = f"[data-cell-id='{cell_id}']"

    # ── canvas ──────────────────────────────────────────────────────────
    def open(self):
        """Bring the cell into view and open its editor.

        The canvas is a transformed scene, so a cell can exist in the DOM
        far outside the viewport: walk the stack strip with the app's own
        Next-stack key until its box is on screen. The focus scrim eats the
        first click, hence two. Both land a third of the way down the cell,
        in its content but clear of the middle: an empty tab centres its
        "Open existing or create a new file" button there, and a second
        click on it opens the cell's Files panel, whose sheet takes every
        later click.
        """
        el = self.page.locator(self.sel).first
        try:
            el.wait_for(timeout=8000)
        except Exception:      # noqa: BLE001 — the cell was minted after load
            # The canvas grows over the channel SSE stream, but a cell created
            # between page load and here can still be missing; one reload is
            # cheaper (and far more reliable) than racing the stream.
            self.page.reload(wait_until="domcontentloaded")
            time.sleep(2)
            el.wait_for(timeout=25000)
        for _ in range(14):
            box = el.bounding_box()
            width = self.page.viewport_size["width"]
            if box and box["x"] >= 0 and box["x"] + box["width"] <= width:
                break
            self.page.keyboard.press("Control+ArrowRight")
            time.sleep(0.6)
        box = el.bounding_box()
        spot = {"x": box["width"] / 2, "y": box["height"] * 0.3}
        el.click(position=spot)
        time.sleep(0.8)
        el.click(position=spot)
        time.sleep(1.5)
        return self

    def shot(self, suffix: str) -> str:
        return self.ev.shot(self.page, f"{self.name}.{suffix}")

    # ── the chat renderer (shadow DOM: no CSS selectors reach in) ────────
    def chat_mounted(self) -> bool:
        return bool(self.page.evaluate("() => !!(%s)" % self.host))

    def reopen(self):
        """Reload the page and re-open the cell.

        The recovery for a renderer whose ES module never arrived: starting a
        container flips the host's network stack, Chromium aborts every
        in-flight fetch with ``ERR_NETWORK_CHANGED``, and the shadow host is
        then permanently empty ("NativeRenderer mount failed"). A reload
        re-fetches it; nothing in the session is lost, since the run lives
        server-side.
        """
        self.page.reload(wait_until="domcontentloaded")
        time.sleep(3)
        return self.open()

    def show_files(self):
        """Show the cell's Files pane: while it is hidden, the tab bar leads
        with its "Show files" toggle."""
        try:
            show = self.page.locator(self.sel).get_by_role("button", name="Show files", exact=True)
            if show.count():
                show.first.click(timeout=5000)
                time.sleep(0.6)
        except Exception:  # noqa: BLE001 — the pane may already be open
            pass
        return self

    def _tree_row(self, path: str, kind: str):
        return self.page.locator(
            f"{self.sel} [data-filenode-kind='{kind}'][data-filenode-path='{path}']")

    def open_path(self, path: str):
        """Open ``path`` from the cell's Files pane: show the pane, open each
        folder on the way down (only while its child is not shown: a click
        on an open folder would close it), then click the file's row — the
        user gesture that opens its tab and activates it."""
        self.show_files()
        parts = path.split("/")
        for depth in range(1, len(parts)):
            child = "/".join(parts[:depth + 1])
            kind = "file" if depth == len(parts) - 1 else "folder"
            if not self._tree_row(child, kind).count():
                self._tree_row("/".join(parts[:depth]), "folder").first.click(timeout=10000)
                time.sleep(0.6)
        self._tree_row(path, "file").first.click(timeout=15000)
        time.sleep(1.0)
        return self

    def chat_pointer_row(self):
        """The tree row of a chat pointer (``_runs/<label>/chat/conversation.chat``)."""
        return self.page.locator(
            f"{self.sel} [data-filenode-kind='file'][data-filenode-path$='/{CHAT_POINTER}']")

    def reveal_chat_pointer(self):
        """Walk the cell's Files pane down to a chat pointer.

        The pane may be hidden with every folder shut, so the leaf is not in
        the DOM to click. Show the pane, then open ``_runs`` and each run
        folder's ``chat`` folder until the leaf appears (a click on an open
        folder would close it, so only shut ones are clicked). With several
        runs the first run folder holding a chat wins; ``open_path`` is exact.
        """
        self.show_files()
        leaf = self.chat_pointer_row()
        runs = self.page.locator(
            f"{self.sel} [data-filenode-kind='folder'][data-filenode-path^='_runs/']")
        try:
            if not leaf.count() and not runs.count():
                self._tree_row("_runs", "folder").first.click(timeout=5000)
                time.sleep(0.6)
            folders = runs.evaluate_all("els => els.map(e => e.getAttribute('data-filenode-path'))")
            for folder in (f for f in folders if f.count("/") == 1):
                if leaf.count():
                    break
                chat = f"{folder}/chat"
                if not self._tree_row(chat, "folder").count():
                    self._tree_row(folder, "folder").first.click(timeout=5000)
                    time.sleep(0.6)
                if self._tree_row(chat, "folder").count() and not leaf.count():
                    self._tree_row(chat, "folder").first.click(timeout=5000)
                    time.sleep(0.6)
        except Exception:  # noqa: BLE001 — a row may not be in the tree yet
            pass
        return self

    # ── the run light, and the status pane ──────────────────────────────
    def run_light_count(self) -> int:
        """How many live runs the head's run light counts (its
        ``data-run-count``); 0 while it is the idle ring."""
        count = self.page.evaluate(
            "(c) => { const e = document.querySelector(`[data-cell-id='${c}'] "
            "[data-testid='run-light']`); return e ? e.getAttribute('data-run-count') : ''; }",
            self.cell_id)
        return int(count) if str(count).strip().isdigit() else 0

    def open_runs(self):
        """Show the cell's runs: the head's status caret opens the status
        pane (a second click would close it, so only while it is shut), and
        a drilldown or All goes back to the Status view."""
        root = self.page.locator(self.sel)
        if not root.locator("[data-testid='status-view']").count():
            if not root.locator("[data-runs-pane]").count():
                root.locator("[data-testid='runs-pane-toggle']").first.click(timeout=10000)
            back = root.locator("[data-runs-pane] [data-testid='drilldown-back'], "
                                "[data-runs-pane] [data-testid='all-back']")
            if back.count():
                back.first.click(timeout=10000)
            root.locator("[data-testid='status-view']").wait_for(timeout=10000)
            time.sleep(0.4)
        return self

    def close_runs(self):
        """Esc in the Status view closes the status pane."""
        view = self.page.locator(f"{self.sel} [data-testid='status-view']")
        if view.count():
            view.first.focus()
            self.page.keyboard.press("Escape")
            time.sleep(0.3)
        return self

    def runs_rows(self) -> list[dict]:
        """``[{run_id, text, ended}]`` — the Status view's rows: one per live
        run on the cell (the runner's name and short id, whether it listens,
        the elapsed time), then this session's finished runs (``ended``)."""
        self.open_runs()
        return self.page.evaluate(
            "(c) => [...document.querySelectorAll(`[data-cell-id='${c}'] "
            "[data-testid='status-run-row']`)].map(e => ({run_id: "
            "e.getAttribute('data-run-id'), text: e.textContent.trim(), "
            "ended: e.getAttribute('data-ended') === 'true'}))",
            self.cell_id)

    def open_run(self, run_id: str):
        """Open ``run_id``'s drilldown from its row in the Status view."""
        self.open_runs()
        self.page.locator(
            f"{self.sel} [data-testid='status-run-row'][data-run-id='{run_id}']").first.click()
        self.page.locator(
            f"{self.sel} [data-testid='run-drilldown'][data-run-id='{run_id}']").wait_for(timeout=10000)
        time.sleep(0.4)
        return self

    def open_all(self):
        """Open All: every live run's messages, the cell's own notes and the
        cell's log."""
        self.open_runs()
        self.page.locator(f"{self.sel} [data-testid='status-all']").first.click()
        self.page.locator(f"{self.sel} [data-testid='run-all']").wait_for(timeout=10000)
        return self

    def row_buttons(self, run_id: str) -> list[str]:
        """The ``data-testid`` of every button on ``run_id``'s row in the
        Status view (a row carries no Stop: only ``status-run-open-ui`` when
        the run has a file to open). A row that is missing fails here rather
        than reading as a row with no buttons."""
        self.open_runs()
        found = self.page.evaluate(
            "([c, r]) => { const row = document.querySelector(`[data-cell-id='${c}'] "
            "[data-testid='status-run-row'][data-run-id='${r}']`); return row ? "
            "[...row.querySelectorAll('button')].map(e => e.getAttribute('data-testid') || '') : null; }",
            [self.cell_id, run_id])
        assert found is not None, f"run {run_id[:8]} has no row in the Status view"
        return found

    def click_stop(self, run_id: str):
        """Stop ``run_id`` the product's way: open its drilldown, then the
        Stop in its header."""
        self.open_run(run_id)
        self.page.locator(
            f"{self.sel} [data-testid='run-drilldown'] [data-testid='run-stop']").first.click()
        return self

    def messages(self, run_id: str) -> list[dict]:
        """``[{id, read, text}]`` — the messages ``run_id``'s drilldown shows."""
        self.open_run(run_id)
        return self.page.evaluate(
            "(c) => [...document.querySelectorAll(`[data-cell-id='${c}'] [data-testid='run-drilldown'] "
            "[data-testid='annotation-message']`)].map(e => ({id: e.getAttribute('data-id'), "
            "read: e.getAttribute('data-read') === 'true', text: e.textContent.trim()}))",
            self.cell_id)

    # ── tabs, the Files pane and the runners panel ──────────────────────
    def click_tab_button(self, path: str, label: str):
        """Click ``label`` on the toolbar of ``path``'s renderer — a web tab's
        Capture or New tab. The toolbar is in the renderer's shadow root,
        which Playwright's CSS engine pierces; the renderer posts the
        button's action with ``path`` as its source."""
        self.page.locator(f"{self.sel} [data-file-path='{path}'] button",
                          has_text=label).first.click(timeout=15000)
        return self

    def tab(self, path: str):
        """The tab of ``path`` in this cell's tab strips (its title starts
        with the path; a locked one reads ``<path> — <runner> <id>``)."""
        return self.page.locator(f"{self.sel} [role='tab'][title^='{path}']")

    def focus_tab(self, path: str):
        self.tab(path).first.click()
        time.sleep(0.8)
        return self

    def tab_selected(self, path: str) -> bool:
        tab = self.tab(path)
        return bool(tab.count()) and tab.first.get_attribute("aria-selected") == "true"

    def showing(self, path: str) -> bool:
        """Whether a renderer host for ``path`` is on the page (a pane's
        active tab; the host carries ``data-file-path``)."""
        return bool(self.page.locator(f"{self.sel} [data-file-path='{path}']").count())

    def open_from_tree(self, path: str):
        """Open a file from the cell's Files pane (a click on its row opens,
        or activates, its tab)."""
        return self.open_path(path)

    def open_runners(self):
        """Open the cell's runners panel with the Run shortcut (Alt+R), which
        acts on the selected cell (``open`` selected it), and return the
        panel's locator once its list is up."""
        self.page.keyboard.press("Alt+r")
        panel = self.page.locator(f"[data-pick-surface][data-pick-cell='{self.cell_id}']")
        panel.get_by_role("listbox", name="Runners").wait_for()
        return panel

    def open_chat_tab(self, timeout: float = 20):
        """Make sure a chat run's renderer is mounted for this cell.

        A fresh page mounts only the cell's PINNED file; when that is not the
        chat pointer (a renderer that never arrived, another file opened),
        the chat host is reached by opening the pointer from the cell's file
        tree, with one page reload as the fallback.
        """
        for attempt in (0, 1):
            if self.chat_mounted():
                return self
            try:
                self.reveal_chat_pointer().chat_pointer_row().first.click(timeout=10000)
            except Exception:  # noqa: BLE001 — the tab may not be in the tree
                pass
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.chat_mounted():
                    return self
                time.sleep(0.5)
            if attempt == 0:
                self.reopen()
        raise AssertionError("the chat renderer never mounted for this cell")

    def send(self, text: str):
        """Type through the NATIVE value setter, then click Send.

        ``fill``/``type`` do not reach React's controlled textarea inside the
        renderer's shadow root; the native setter plus a bubbling ``input``
        is what the framework listens for.

        The composer's controls churn while a turn streams, so wait for the
        Send button rather than reading ``.pop()`` off an empty list — a send
        that races the re-render used to die as an opaque JS TypeError.
        """
        self.open_chat_tab()
        self.page.wait_for_function(
            """() => { const h = %s;
                 return !!(h && h.shadowRoot.querySelector('textarea')
                        && h.shadowRoot.querySelector(
                             'button[title="Send message"]')); }""" % self.host,
            timeout=20000)
        self.page.evaluate(
            """(t) => { const h = %s;
                 const ta = h.shadowRoot.querySelector('textarea');
                 ta.focus();
                 const set = Object.getOwnPropertyDescriptor(
                     HTMLTextAreaElement.prototype, 'value').set;
                 set.call(ta, t);
                 ta.dispatchEvent(new Event('input', {bubbles: true})); }""" % self.host,
            text)
        time.sleep(0.4)
        self.page.evaluate(
            "() => { const h = %s; [...h.shadowRoot.querySelectorAll("
            "'button[title=\"Send message\"]')].pop().click(); }" % self.host)

    def composer_buttons(self) -> list[str]:
        return self.page.evaluate(
            "() => { const h = %s; return [...h.shadowRoot.querySelectorAll("
            "'button[title]')].map(b => b.getAttribute('title')); }" % self.host)

    def stop_generating(self) -> bool:
        return bool(self.page.evaluate(
            "() => { const h = %s; const b = h.shadowRoot.querySelector("
            "'button[title=\"Stop generating\"]'); if (!b) return false; "
            "b.click(); return true; }" % self.host))

    def selects(self) -> list[dict]:
        return self.page.evaluate(
            """() => { const h = %s; if (!h) return [];
                 return [...h.shadowRoot.querySelectorAll('select')].map(s => ({
                   label: (s.parentElement && s.parentElement.innerText || '')
                            .split('\\n')[0],
                   value: s.value, disabled: s.disabled,
                   options: [...s.options].map(o => o.value)})); }""" % self.host)

    def pick(self, label_prefix: str, value: str):
        """Change one config select the way a user does (a change event the
        renderer forwards as a config pick)."""
        self.open_chat_tab()
        ok = self.page.evaluate(
            """(a) => { const h = %s;
                 const s = [...h.shadowRoot.querySelectorAll('select')].find(
                   x => ((x.parentElement && x.parentElement.innerText) || '')
                          .startsWith(a[0]));
                 if (!s) return false;
                 const set = Object.getOwnPropertyDescriptor(
                     HTMLSelectElement.prototype, 'value').set;
                 set.call(s, a[1]);
                 s.dispatchEvent(new Event('change', {bubbles: true}));
                 return s.value === a[1]; }""" % self.host,
            [label_prefix, value])
        assert ok, f"no {label_prefix!r} select offering {value!r}: {self.selects()}"
        # The select disables itself while the pick applies.
        for _ in range(60):
            if not any(s["disabled"] for s in self.selects()):
                return
            time.sleep(0.5)

    def attach(self, file_path: str):
        with self.page.expect_file_chooser() as chooser:
            self.page.evaluate(
                "() => { const h = %s; h.shadowRoot.querySelector("
                "'button[title=\"Attach files\"]').click(); }" % self.host)
        chooser.value.set_files(file_path)
        time.sleep(2)

    # ── the terminal renderer ───────────────────────────────────────────
    #
    # xterm.js runs its CANVAS renderer here: the screen is painted into
    # ``.xterm-text-layer`` / ``.xterm-cursor-layer`` canvases and there is no
    # ``.xterm-rows`` DOM to read. Terminal OUTPUT is therefore invisible to
    # the driver — every assertion about what the TTY said has to be made on
    # the run's terminal transcript (a run log artifact, which is the
    # contract anyway), and readiness is measured structurally.

    def terminal_mounted(self) -> bool:
        """The TTY renderer is mounted and laid out (a screen with a box)."""
        return bool(self.page.evaluate(
            """() => { const h = [...document.querySelectorAll("%s *")].find(
                   e => e.shadowRoot && e.shadowRoot.querySelector('.xterm'));
                 if (!h) return false;
                 const s = h.shadowRoot.querySelector('.xterm-screen');
                 if (!s) return false;
                 const r = s.getBoundingClientRect();
                 return r.width > 40 && r.height > 40; }""" % self.sel))

    def terminal_text(self) -> str:
        """Best effort only — EMPTY under the canvas renderer.

        Kept so nobody rediscovers this the hard way (the 2026-08-29 QA round
        wrote 0-byte terminal dumps for exactly this reason). Use it for
        evidence, never for an assertion.
        """
        return self.page.evaluate(
            """() => { const h = [...document.querySelectorAll(
                   "%s *")].find(e => e.shadowRoot &&
                   e.shadowRoot.querySelector('.xterm'));
                 if (!h) return '';
                 return [...h.shadowRoot.querySelectorAll('.xterm-rows > div')]
                   .map(d => d.innerText).join('\\n'); }""" % self.sel)

    def xterm_click_point(self):
        return self.page.evaluate(
            """() => { const h = [...document.querySelectorAll(
                   "%s *")].find(e => e.shadowRoot &&
                   e.shadowRoot.querySelector('.xterm'));
                 if (!h) return null;
                 const s = h.shadowRoot.querySelector('.xterm-screen')
                        || h.shadowRoot.querySelector('.xterm');
                 const r = s.getBoundingClientRect();
                 return [r.x + r.width / 2, r.y + r.height * 0.8]; }""" % self.sel)

    def type_in_terminal(self, text: str):
        point = self.xterm_click_point()
        assert point, "no xterm surface mounted in this cell"
        self.page.mouse.click(point[0], point[1])
        time.sleep(0.8)
        self.page.keyboard.type(text, delay=25)
        time.sleep(0.6)
        self.page.keyboard.press("Enter")


@pytest.fixture
def cell_view(page, evidence, request):
    """``cell_view(cell_id)`` → an opened :class:`CellView`."""
    def make(cell_id: str) -> CellView:
        return CellView(page, cell_id, evidence, request.node.name).open()
    return make
