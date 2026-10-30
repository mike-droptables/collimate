# SPDX-License-Identifier: MIT
"""Host-terminal helpers shared by the live terminal tests.

A terminal leg's TTY served for real: :class:`_HostTerminal` runs one
``docker exec -it`` on a host PTY the way the app's terminal route does for a
container session, or, for a session on this machine, the argv itself as its
own session with the PTY as its controlling terminal, and
:func:`_host_terminals` swaps the recording bridge's terminal service (both
forms) for it. Plus the small helpers the terminal tests share: typing at a TUI
(:func:`_type_line`), walking a fresh harness home's first-run screens to
its prompt (:func:`_up_to_prompt`, :func:`_agy_opens_at_its_prompt`), and
finding where a held provider key landed (:func:`_key_values`,
:func:`_key_in_home`) — names only, never values.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import threading
import time

from .conftest import KEY_ENV, model_for


def _run(coro):
    return asyncio.run(coro)


def _teardown_acp():
    from _agent_shared.acp_run import close_acp_sessions_sync
    close_acp_sessions_sync()


def _leg_model(provider: str, cheap: "dict | None" = None) -> str:
    """The model a leg bills: the suite's ``COLLIMATE_LIVE_MODEL_<PROVIDER>``
    override when set, else the caller's cheaper pick for the harness, else the
    engine default (:func:`conftest.model_for`)."""
    if (os.environ.get(f"COLLIMATE_LIVE_MODEL_{provider.upper()}") or "").strip():
        return model_for(provider)
    return (cheap or {}).get(provider) or model_for(provider)


# Escape sequences, stripped to read what a screen says.
_ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)"
                   rb"|[P_^][^\x1b]*\x1b\\|[()][0-9A-B]|.)")


def _controlling_terminal() -> None:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class _HostTerminal:
    """One terminal as the host's terminal route runs it (the app's
    ``api/routes/terminal.py``), answering the terminal queries xterm.js
    answers. A container session (``container_id``) is ``docker exec -i -t
    -w <cwd> -e NAME … -- <container> <command>`` on a host PTY, the env's
    values in the client's own environment (never in an argv); :meth:`stop`
    then ends the client only, as the host's stop does: the program inside
    is the leg's to end. A session on this machine (``container_id=None``)
    is the command itself, its own session with the PTY as its controlling
    terminal, in ``cwd`` with ``env`` as its whole environment; :meth:`stop`
    kills its process group, as the host's destroy does."""

    def __init__(self, container_id: "str | None", command: list, cwd: str, env: dict):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        values = {k: str(v) for k, v in (env or {}).items()}
        self.host = container_id is None
        if self.host:
            self.proc = subprocess.Popen(
                list(command), cwd=cwd, env=values, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, preexec_fn=_controlling_terminal)
        else:
            argv = ["docker", "exec", "-i", "-t", "-w", cwd or "/workspace"]
            for name in values:
                argv += ["-e", name]
            self.proc = subprocess.Popen(
                [*argv, "--", container_id, *command], env={**os.environ, **values},
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self._fd = master       # the pump's to close, once the client is gone
        self._out = bytearray()
        self._read = 0          # the output wait_for has consumed
        self._lock = threading.Lock()
        self._pumping = threading.Thread(target=self._pump, daemon=True)
        self._pumping.start()

    def _pump(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self._fd, 65536)
                except OSError:     # EIO: the client exited
                    return
                if not chunk:
                    return
                with self._lock:
                    self._out += chunk
                if b"\x1b[c" in chunk:                                # DA1
                    self.send(b"\x1b[?62;22c")
                for m in re.finditer(rb"\x1b\[\?(\d+)\$p", chunk):   # DECRQM
                    self.send(b"\x1b[?" + m.group(1) + b";2$y")
                if b"\x1b[?u" in chunk:                               # kitty keys
                    self.send(b"\x1b[?0u")
                if b"\x1b[6n" in chunk:                               # cursor
                    self.send(b"\x1b[1;1R")
        finally:
            # Closed here and only here, under the lock send takes: a closed
            # descriptor's number is reused at once (by a docker socket), and
            # neither a late read nor a late write may reach that.
            with self._lock:
                os.close(self._fd)
                self._fd = -1

    def send(self, data: bytes) -> None:
        with self._lock:
            if self._fd < 0:
                raise AssertionError(f"the terminal's client exited "
                                     f"({self.proc.poll()}) before {data!r}")
            os.write(self._fd, data)

    def wait_for(self, *patterns, timeout: float = 120.0):
        """Wait for the first of ``patterns`` (bytes, or a compiled bytes
        regex) in the output not consumed yet, consume through it, and return
        the pattern that matched. Fails with the screen's recent text when
        none appears in ``timeout``."""
        rxs = [p if isinstance(p, re.Pattern) else re.compile(re.escape(p))
               for p in patterns]
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                out = bytes(self._out[self._read:])
            hits = [(m.start(), m.end(), p)
                    for p, rx in zip(patterns, rxs) if (m := rx.search(out))]
            if hits:
                _start, end, pattern = min(hits, key=lambda h: h[0])
                self._read += end
                return pattern
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"the terminal never showed any of {patterns} (client "
                    f"exit {self.proc.poll()}); it shows: {self.recent()!r}")
            time.sleep(0.2)

    def settle(self, quiet: float = 1.5, timeout: float = 30.0) -> None:
        """Wait until the program has printed nothing for ``quiet`` seconds
        (it has drawn what it was sent), and consume what it printed. A TUI's
        echo of typed input cannot be matched as text — it repaints a few
        keys at a time, between redraws of other cells — so what was typed is
        checked where the program keeps it instead."""
        deadline = time.monotonic() + timeout
        seen, since = -1, time.monotonic()
        while True:
            with self._lock:
                size = len(self._out)
            if size != seen:
                seen, since = size, time.monotonic()
            elif time.monotonic() - since >= quiet:
                self._read = size
                return
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"the terminal never settled (client exit "
                    f"{self.proc.poll()}); it shows: {self.recent()!r}")
            time.sleep(0.1)

    def said(self) -> str:
        """Everything the program printed, its escape sequences stripped."""
        with self._lock:
            out = bytes(self._out)
        return _ANSI.sub(b"", out).decode("utf-8", "replace")

    def recent(self, chars: int = 1500) -> str:
        return " ".join(self.said().split())[-chars:]

    def stop(self) -> None:
        """End the client (idempotent); the pump then closes the PTY. A
        session on this machine loses its whole process group (its pid IS
        the group), the leader gone or not."""
        if self.host:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    break
                try:
                    self.proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        elif self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        self._pumping.join(timeout=10)

    def group(self) -> list:
        """The pids still in this session's process group (a session on this
        machine), read from ``/proc``."""
        found = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat") as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[2]) == self.proc.pid and fields[0] != "Z":
                found.append(int(name))
        return found


def _host_terminals(bridge, monkeypatch) -> dict:
    """Serve the run's terminals for real: the recording bridge only writes a
    ``start_terminal`` (or ``start_host_terminal``) down; here each one is a
    :class:`_HostTerminal`, its status is its client's, and a stop ends the
    client (a session on this machine: its process group)."""
    terms: dict = {}

    def start_terminal(*, container_id, command, cwd=None, env=None,
                       keep_alive=False):
        sid = f"live-term-{len(terms) + 1}"
        terms[sid] = _HostTerminal(container_id, list(command), cwd or "",
                                   env or {})
        return {"terminal_id": sid, "ws_path": "", "token": ""}

    def start_host_terminal(*, command, cwd=None, env=None, keep_alive=False):
        return start_terminal(container_id=None, command=command, cwd=cwd, env=env)

    def stop_terminal(terminal_id):
        if terminal_id in terms:
            terms[terminal_id].stop()

    def terminal_status(terminal_id):
        term = terms.get(terminal_id)
        code = None if term is None else term.proc.poll()
        return {"exists": term is not None,
                "ended": term is None or code is not None, "exit_code": code}

    monkeypatch.setattr(bridge, "start_terminal", start_terminal)
    monkeypatch.setattr(bridge, "start_host_terminal", start_host_terminal)
    monkeypatch.setattr(bridge, "stop_terminal", stop_terminal)
    monkeypatch.setattr(bridge, "terminal_status", terminal_status)
    return terms


def _type_line(term: _HostTerminal, text: str) -> None:
    """Type ``text`` at the prompt and submit it once the TUI has drawn it
    (the conversation record then shows what arrived)."""
    term.send(text.encode())
    term.settle()
    term.send(b"\r")


# ══════════════════════════════════════════════════════════════════════
# Each TUI up to its prompt
# ══════════════════════════════════════════════════════════════════════

# agy's first-run screens (1.2.11), and its prompt.
_AGY_SCREENS = {
    b"Choose your color scheme": "the colour-scheme screen",
    b"Terms of Service & Data Use": "the Terms of Service & Data Use screen",
    b"Do you trust the contents of this project?": "the folder-trust screen",
}
_AGY_PROMPT = b"? for shortcuts"
# Under google, agy draws its welcome with this line and a "Signing in..."
# spinner WHILE it tries the leased login silently (probed with a rejected
# token), so a login it goes on to accept may pass through the same frame:
# the text is no verdict. It only names the failure once the prompt never
# came.
_AGY_SIGNED_OUT = "You are currently not signed in"


def _agy_opens_at_its_prompt(term: _HostTerminal) -> None:
    """agy's TUI opens straight at its prompt: the runner wrote the first-run
    answers before the container started (onboarding complete, which skips
    the colour and data-use screens; the workspace trusted), so none of the
    three screens a new harness home would otherwise show appears. The first
    of the screens or the prompt to show is what agy drew first."""
    try:
        first = term.wait_for(*_AGY_SCREENS, _AGY_PROMPT, timeout=180)
    except AssertionError:
        if _AGY_SIGNED_OUT in term.said():
            raise AssertionError(
                "agy stayed on its sign-in screen (the leased login was "
                f"rejected); it shows: {term.recent()!r}") from None
        raise
    assert first == _AGY_PROMPT, (
        f"agy showed {_AGY_SCREENS[first]} instead of opening at its prompt; "
        f"it shows: {term.recent()!r}")
    said = term.said()
    shown = [name for text, name in _AGY_SCREENS.items() if text.decode() in said]
    assert not shown, f"agy drew {shown} before its prompt"


# The screens a fresh harness home shows before its prompt, and what a user
# who takes the defaults answers — per harness, (pattern, keys) in the order
# they may appear. The prompt pattern ends the walk.
_SCREENS: dict = {
    "qwen-code": [],
    "pi": [],
    # A promotion cline 3.0.65 lays over its prompt: any key but Enter
    # closes it. It usually lands a few seconds AFTER the prompt is drawn
    # (:data:`_AFTER_PROMPT`); listed here too for when it comes first.
    "cline": [(re.compile(rb"any other key to close"), [b"\x1b"])],
}
# A screen a TUI may lay over its prompt shortly after drawing it — waited
# for (up to the seconds given) and answered before anything is typed, since
# keys typed under it are the overlay's, not the prompt's.
_AFTER_PROMPT: dict = {
    "cline": (re.compile(rb"any other key to close"), b"\x1b", 20.0),
}
_PROMPT: dict = {
    "qwen-code": re.compile(rb"Type your message"),
    "pi": re.compile(rb"\(auto\)"),
    "cline": re.compile(rb"What can I do for you"),
}
# A screen that means the TUI will not reach its prompt without the user
# doing what the product promised to spare them — reported, with the reason.
_BLOCKED: dict = {
    # (cline's provider onboarding was here until its runner wrote the key
    # into the TTY's own providers.json: cline 3.0.65's TUI reads no
    # CLINE_PROVIDER / CLINE_API_KEY / CLINE_MODEL.)
}

# What some TUIs keep redrawing: a braille logo, and a working spinner
# with a seconds counter.
_ANIMATION = re.compile(r"[\u2800-\u28ff]+|(?:\u25c6\s*\d*\s*){2,}")


def _shown(term: _HostTerminal, chars: int = 800) -> str:
    """The screen's recent text, animation stripped."""
    return " ".join(_ANIMATION.sub(" ", term.said()).split())[-chars:]


def _up_to_prompt(backend: str, term: _HostTerminal) -> tuple:
    """Answer ``backend``'s first-run screens until its prompt is up. Returns
    ``(screens seen, why the TUI is blocked or "")``."""
    screens = [p for p, _keys in _SCREENS[backend]]
    answers = dict(_SCREENS[backend])
    blocked, why = _BLOCKED.get(backend, (None, ""))
    seen: list = []
    while True:
        hit = term.wait_for(*screens, *([blocked] if blocked else []),
                            _PROMPT[backend], timeout=240)
        if hit is _PROMPT[backend]:
            seen.append("prompt")
            if backend in _AFTER_PROMPT:
                overlay, keys, wait = _AFTER_PROMPT[backend]
                try:
                    term.wait_for(overlay, timeout=wait)
                except AssertionError:
                    pass
                else:
                    seen.append(f"then {overlay.pattern.decode()}")
                    time.sleep(1.5)
                    term.send(keys)
                    term.settle(quiet=2.0)
            return seen, ""
        seen.append(hit.pattern.decode())
        if hit is blocked:
            return seen, why
        if seen.count(seen[-1]) == 1:
            seen.append(f"shows: {_shown(term, 300)}")
        # Answered again on every redraw that still carries its title: a
        # TUI that does not take the first answer gets it again until the
        # prompt shows. An extra answer that lands on the empty prompt is
        # inert (Down, Enter on nothing).
        for keys in answers[hit]:
            time.sleep(1.5)
            term.send(keys)


# ══════════════════════════════════════════════════════════════════════
# Where a held key landed (names only)
# ══════════════════════════════════════════════════════════════════════


def _key_values() -> dict:
    """name → value for every provider key this process holds (never
    printed)."""
    out = {}
    for name in sorted(set(KEY_ENV.values())):
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 12:
            out[name] = value
    return out


def _key_in_home(backend: str) -> tuple:
    """Files in ``backend``'s per-cell harness home holding a held key's
    value, as ``(relative path, key name, the JSON fields just before it)``
    — names only — split into those
    the run carries out of the disposable home (under a session-store path,
    :data:`acp_client._SESSION_PATHS`, and not a credential file name) and
    the rest."""
    from _agent_shared.harness_homes import _CREDENTIAL_NAMES, _session_paths, harness_home

    home = harness_home(backend)
    surfaces = _session_paths(backend)
    values = _key_values()
    carried, kept = [], []
    for dirpath, _dirs, files in os.walk(home):
        for fname in files:
            path = os.path.join(dirpath, fname)
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(path, home)
            out = carried if (fname not in _CREDENTIAL_NAMES and any(
                rel == s or rel.startswith(s + os.sep) for s in surfaces)) else kept
            out.extend((rel, name, _fields_before(blob, value.encode()))
                       for name, value in values.items() if value.encode() in blob)
    return carried, kept


def _fields_before(blob: bytes, value: bytes) -> list:
    """The last few JSON field names before each place ``value`` sits in
    ``blob`` — where a record keeps it, without showing it."""
    names = []
    start = 0
    while (at := blob.find(value, start)) >= 0 and len(names) < 3:
        before = blob[max(0, at - 400):at].decode("utf-8", "replace")
        names.append(re.findall(r'"([A-Za-z_][\w.-]{0,40})"\s*:', before)[-3:])
        start = at + len(value)
    return names
