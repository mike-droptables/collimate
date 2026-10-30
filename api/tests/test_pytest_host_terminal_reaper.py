# SPDX-License-Identifier: MIT
"""The startup sweep of host terminals a killed server left running: every
process labelled with this instance and a server process that is gone has
its group killed (``startup_cleanup.reap_orphan_host_terminals``); another
instance's, a live server's and an unreadable label are left alone."""
from __future__ import annotations

import os
import subprocess
import time

import startup_cleanup
from sessions.terminal import HOST_TERMINAL_LABEL


def test_only_this_instances_labels_whose_server_is_gone_are_orphans():
    alive = {100, 200}.__contains__
    labelled = [
        (1, "mine:999:run-a"),          # this instance, its server gone
        (2, "mine:100:run-b"),          # this instance, a live server
        (3, "other:999:run-c"),         # another instance
        (4, "mine:999"),                # malformed
        (5, "mine:notapid:run-d"),
        (6, "mine:999:"),
    ]
    assert startup_cleanup.orphan_host_terminal_pids(labelled, "mine", alive) == [1]


def test_the_sweep_kills_a_stray_group_and_spares_the_rest(monkeypatch):
    mine = startup_cleanup.instance_id()
    dead_server = subprocess.Popen(["true"])
    dead_server.wait()

    def spawn(label: str) -> subprocess.Popen:
        return subprocess.Popen(["sh", "-c", "sleep 300 & wait"], start_new_session=True,
                                env={"PATH": "/usr/bin:/bin", HOST_TERMINAL_LABEL: label})

    stray = spawn(f"{mine}:{dead_server.pid}:run-stray")
    live = spawn(f"{mine}:{os.getpid()}:run-live")
    foreign = spawn(f"not-{mine}:{dead_server.pid}:run-foreign")
    try:
        time.sleep(0.3)
        assert startup_cleanup.reap_orphan_host_terminals() >= 2   # the leader and its sleep
        stray.wait(timeout=5)
        assert stray.returncode is not None
        for proc in (live, foreign):
            assert proc.poll() is None
    finally:
        for proc in (stray, live, foreign):
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            proc.wait(timeout=5)
