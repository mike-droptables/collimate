"""Coverage for ProcessManager._build_preexec_fn — subprocess rlimits.

The preexec_fn runs in the forked child before exec; we can't directly
test it post-exec without spawning a real subprocess. Instead, the unit
tests verify the *function* the manager builds: that it captures the
right limits from the manifest, falls back to defaults, and tolerates
setrlimit failures.
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest


def test_preexec_uses_defaults_when_manifest_silent():
    from process_manager.manager import ProcessManager
    seen_calls: list[tuple[int, int]] = []

    fake_resource = type("R", (), {
        "RLIMIT_DATA": 2,
        "RLIMIT_CPU": 0,
        "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(
            lambda which, lim: seen_calls.append((which, lim[0]))
        ),
    })

    with patch.dict("sys.modules", {"resource": fake_resource}):
        fn = ProcessManager._build_preexec_fn({})
        assert fn is not None
        fn()

    by_which = dict(seen_calls)
    assert by_which[fake_resource.RLIMIT_DATA]     == ProcessManager._DEFAULT_RLIMIT_MEMORY_BYTES
    assert by_which[fake_resource.RLIMIT_CPU]    == ProcessManager._DEFAULT_RLIMIT_CPU_SECONDS
    assert by_which[fake_resource.RLIMIT_NOFILE] == ProcessManager._DEFAULT_RLIMIT_OPEN_FILES


def test_preexec_honors_manifest_overrides():
    from process_manager.manager import ProcessManager
    seen_calls: list[tuple[int, int]] = []
    fake_resource = type("R", (), {
        "RLIMIT_DATA": 2,
        "RLIMIT_CPU": 0,
        "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(
            lambda which, lim: seen_calls.append((which, lim[0]))
        ),
    })

    manifest = {
        "rlimits": {
            "memory_bytes": 4 * 1024 * 1024 * 1024,  # 4 GB
            "cpu_seconds": 900,                       # 15 min
            "open_files": 1024,
        }
    }
    with patch.dict("sys.modules", {"resource": fake_resource}):
        fn = ProcessManager._build_preexec_fn(manifest)
        fn()

    by_which = dict(seen_calls)
    assert by_which[fake_resource.RLIMIT_DATA]     == 4 * 1024 * 1024 * 1024
    assert by_which[fake_resource.RLIMIT_CPU]    == 900
    assert by_which[fake_resource.RLIMIT_NOFILE] == 1024


def test_preexec_ignores_invalid_manifest_values():
    """Negative / zero / non-numeric values fall back to the default
    rather than raising or applying nonsense to the child."""
    from process_manager.manager import ProcessManager
    seen_calls: list[tuple[int, int]] = []
    fake_resource = type("R", (), {
        "RLIMIT_DATA": 2,
        "RLIMIT_CPU": 0,
        "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(
            lambda which, lim: seen_calls.append((which, lim[0]))
        ),
    })

    manifest = {
        "rlimits": {
            "memory_bytes": -1,
            "cpu_seconds": "not a number",
            "open_files": 0,
        }
    }
    with patch.dict("sys.modules", {"resource": fake_resource}):
        fn = ProcessManager._build_preexec_fn(manifest)
        fn()

    by_which = dict(seen_calls)
    assert by_which[fake_resource.RLIMIT_DATA]     == ProcessManager._DEFAULT_RLIMIT_MEMORY_BYTES
    assert by_which[fake_resource.RLIMIT_CPU]    == ProcessManager._DEFAULT_RLIMIT_CPU_SECONDS
    assert by_which[fake_resource.RLIMIT_NOFILE] == ProcessManager._DEFAULT_RLIMIT_OPEN_FILES


def test_preexec_swallows_setrlimit_failures():
    """A kernel that refuses one rlimit (e.g. user already has a lower
    hard limit) shouldn't tank the whole exec — that would block legit
    runs on locked-down hosts."""
    from process_manager.manager import ProcessManager

    def _raise(which, lim):
        raise OSError("operation not permitted")

    fake_resource = type("R", (), {
        "RLIMIT_DATA": 2,
        "RLIMIT_CPU": 0,
        "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(_raise),
    })

    with patch.dict("sys.modules", {"resource": fake_resource}):
        fn = ProcessManager._build_preexec_fn({})
        # Calling fn must NOT raise.
        fn()


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_DATA accounting of mappings is Linux")
def test_default_memory_cap_admits_address_space_reservations_and_refuses_committed_memory():
    """The cap binds committed memory, never address space: a V8-sandbox-sized
    PROT_NONE reservation (what Codex's code-mode host and Chromium make when
    an isolate starts, under whatever rlimits the generator passes down) goes
    through under the default cap, while committing more than the cap does
    not. Under an address-space cap the reservation itself failed and the
    host died with SIGTRAP before its first tool call."""
    from process_manager.manager import ProcessManager

    child = (
        "import mmap, sys\n"
        "mmap.mmap(-1, 1 << 40, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS, prot=0)\n"
        "try:\n"
        "    mmap.mmap(-1, 9 << 30, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,\n"
        "              prot=mmap.PROT_READ | mmap.PROT_WRITE)\n"
        "except OSError:\n"
        "    sys.exit(3)\n"
        "sys.exit(4)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        preexec_fn=ProcessManager._build_preexec_fn({}),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 3, proc


def test_hard_wallclock_cap_is_an_attribute():
    """The hard cap is a class-level constant configurable via env var.
    Tests of the timeout logic depend on it being readable as
    ``ProcessManager._HARD_WALLCLOCK_CAP_SECONDS``."""
    from process_manager.manager import ProcessManager
    assert isinstance(ProcessManager._HARD_WALLCLOCK_CAP_SECONDS, int)
    assert ProcessManager._HARD_WALLCLOCK_CAP_SECONDS > 0


def test_an_interactive_session_is_not_capped_by_cumulative_cpu__F0211(monkeypatch):
    """An interactive (``chat: true``) generator's budget bounds one turn:
    the wallclock watchdog restarts its clock whenever the session returns
    to awaiting input. RLIMIT_CPU is cumulative over the process's life, so
    a default CPU cap would kill a healthy long session once its turns
    together used it up, however short each turn was. Here every turn stays
    under a 1 s cap while the session uses more than that in total."""
    from process_manager.manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "_DEFAULT_RLIMIT_CPU_SECONDS", 1)
    session = (
        "import time\n"
        "for turn in range(3):\n"
        "    start = time.process_time()\n"
        "    while time.process_time() - start < 0.6:\n"
        "        pass\n"
        "    print('turn', turn, flush=True)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", session],
        preexec_fn=ProcessManager._build_preexec_fn({"chat": True}),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc
    assert proc.stdout.split("\n")[:3] == ["turn 0", "turn 1", "turn 2"]


def test_an_interactive_session_keeps_an_explicit_cpu_cap__F0211():
    """The manifest author's own ``rlimits.cpu_seconds`` still applies to an
    interactive generator; only the host default is left off."""
    from process_manager.manager import ProcessManager
    seen_calls: list[tuple[int, int]] = []
    fake_resource = type("R", (), {
        "RLIMIT_DATA": 2,
        "RLIMIT_CPU": 0,
        "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(
            lambda which, lim: seen_calls.append((which, lim[0]))
        ),
    })
    with patch.dict("sys.modules", {"resource": fake_resource}):
        ProcessManager._build_preexec_fn({"chat": True})()
        by_which = dict(seen_calls)
        assert fake_resource.RLIMIT_CPU not in by_which
        assert by_which[fake_resource.RLIMIT_DATA] == ProcessManager._DEFAULT_RLIMIT_MEMORY_BYTES
        seen_calls.clear()
        ProcessManager._build_preexec_fn({"chat": True, "rlimits": {"cpu_seconds": 900}})()
        assert dict(seen_calls)[fake_resource.RLIMIT_CPU] == 900
