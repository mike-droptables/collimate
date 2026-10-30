"""The host `docker exec` a terminal session spawns: what reaches its argv.

A run names the container, command, cwd and env of a terminal session; the
host then runs `docker exec` with them. The container must be one of the
run's own environments and its id must never be read as a docker flag, and
the env's values (provider API keys) must never sit on the argv, which any
local user can read in /proc/<pid>/cmdline.
"""
from __future__ import annotations

from sessions import terminal as terminal_sessions

import asyncio
import json
import os

import pytest

from routes import terminal as term


def _session(**overrides):
    params = {"run_id": "run-1", "cell_id": "cell-x", "container_id": "abc123", "command": ["bash"],
              "cwd": "/work", "env": {}, "keep_alive": False, **overrides}
    info = terminal_sessions.register_session(**params)
    return terminal_sessions._sessions.pop(info["terminal_id"])


@pytest.mark.parametrize("container_id", ["--privileged", "-u", "a b", "x;y"])
def test_container_id_cannot_be_a_docker_flag(container_id):
    with pytest.raises(ValueError, match="container"):
        _session(container_id=container_id)


def test_bad_env_names_and_relative_cwd_are_refused():
    with pytest.raises(ValueError, match="environment variable"):
        _session(env={"A=B": "x"})
    with pytest.raises(ValueError, match="absolute"):
        _session(cwd="work")


def test_container_id_follows_end_of_flags():
    argv, _ = term._exec_argv_and_env(_session())
    assert argv[argv.index("--") + 1:] == ["abc123", "bash"]


def test_secret_values_travel_in_the_client_env_not_the_argv():
    session = _session(env={"TERM": "xterm-256color", "CODEX_API_KEY": "FAKE-KEY-999",
                            "HOME": "/home/coder"})
    argv, child_env = term._exec_argv_and_env(session)
    assert not any("FAKE-KEY-999" in part for part in argv)
    assert ["-e", "CODEX_API_KEY"] == argv[argv.index("CODEX_API_KEY") - 1:argv.index("CODEX_API_KEY") + 1]
    assert child_env["CODEX_API_KEY"] == "FAKE-KEY-999"
    # Names the docker client reads for itself stay inline, so a container
    # HOME never points the host CLI at another config or context.
    assert "HOME=/home/coder" in argv
    assert child_env.get("HOME") == os.environ.get("HOME")


def test_the_container_must_be_one_of_the_runs_environments(tmp_path):
    """The run's container_id is a claim: the host docker-execs only into a
    container of the run's own environments, by the id it placed, and
    refuses and logs anything else."""
    from sessions import terminal as terminal_sessions
    from test_pytest_environments import refusals, run_with_environment

    run, own = run_with_environment(tmp_path)
    run.cell_id = "cell-own"

    def open_terminal(container_id):
        asyncio.run(run._handle_ipc(json.dumps({"req_id": "test-request",
            "op": "create_terminal_session",
            "container_id": container_id, "command": ["bash"],
        })))
        return run._ipc_writer.frames[-1]

    before = set(terminal_sessions._sessions)
    refused = open_terminal("collimate-run-other")
    assert (refused["ok"], refused["reason"]) == (False, "not_owned")
    assert set(terminal_sessions._sessions) == before
    assert any("run-own" in line and "collimate-run-other" in line for line in refusals(run))

    ack = open_terminal(own.name)
    assert ack["ok"] is True
    assert terminal_sessions._sessions.pop(ack["terminal_id"]).container_id == own.id
