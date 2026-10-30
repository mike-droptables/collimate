# SPDX-License-Identifier: MIT
"""F1669: the host keeps only the TAIL of an exec's output past its cap.
Codex teardown safe-opens its host rollout; helpers that need container
files, such as goose's session export and workspace context writeback,
use ``DockerEnvironment.read_file`` instead of ``cat``, so a
day-long session's record keeps its head and a user's file is never written
back as its tail. The stand-in container here caps exec output the way the
host does (with a tiny cap), so a reader that went back to ``cat`` fails."""
from __future__ import annotations

import base64
import os
import shlex
import types

import pytest


from _agent_shared import cli_container as cc  # noqa: E402
from _agent_shared.harnesses.codex import backend as cb  # noqa: E402
from _agent_shared.harnesses.goose import backend as gb  # noqa: E402

_CAP = 64


def _tail(out: str) -> str:
    if len(out) <= _CAP:
        return out
    kept = out[-(_CAP - 40):]
    return f"[... {len(out) - len(kept)} earlier bytes of output dropped]\n" + kept


class _CappedEnv:
    """Files in a dict; ``exec`` answers ``cat``, ``rm -f``, a ``find`` and
    a redirected goose export, its output capped like the host's;
    ``read_file`` returns the whole file; writes use the same env plane."""

    def __init__(self, files=None, *, find="", export=None, unreadable=()):
        self.files = dict(files or {})
        self.find, self.export, self.unreadable = find, export, set(unreadable)
        self.container = self

    def exec(self, argv, **kw):
        if argv[0] == "cat":
            return 0, _tail(self.files[argv[1]])
        if argv[:2] == ["rm", "-f"]:
            self.files.pop(argv[2], None)
            return 0, ""
        cmd = argv[-1]
        if cmd.startswith("rm -f "):
            self.files.pop(shlex.split(cmd)[2], None)
            return 0, ""
        if "find " in cmd:
            return 0, self.find + "\n"
        if "session export" in cmd:
            words = shlex.split(cmd)
            self.files[words[words.index(">") + 1]] = self.export
            return 0, _tail(self.export)
        result = self._write(argv, **kw)
        return result.exit_code, result.output.decode()

    def read_file(self, path, **kw):
        if path in self.unreadable:
            raise OSError(f"cannot read {path} (exit 4)")
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path].encode("utf-8")

    def _write(self, argv, **kw):
        cmd = argv[-1]
        b64 = cmd.split("echo ", 1)[1].split(" | base64", 1)[0]
        path = shlex.split(cmd.rsplit("> ", 1)[1])[0]
        self.files[path] = base64.b64decode(shlex.split(b64)[0]).decode("utf-8")
        return types.SimpleNamespace(exit_code=0, output=b"")


def _long(label: str) -> str:
    return "".join(f'{{"n": {i}, "from": "{label}"}}\n' for i in range(200))


def test_codex_reads_its_rollout_whole__F1669(tmp_path, monkeypatch):
    """The host teardown reads the complete safe-open rollout, never an exec tail."""
    from collimate import fs
    rollout = _long("codex")
    marker = tmp_path / cb._START_MARKER
    marker.write_text("")
    os.utime(marker, ns=(1, 1))
    path = tmp_path / "sessions/2026/09/28/rollout.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(rollout)
    opened = []
    original = fs.open_file
    def open_file(root, relative, **kwargs):
        opened.append((str(root), relative))
        return original(root, relative, **kwargs)
    monkeypatch.setattr(fs, "open_file", open_file)
    assert len(rollout) > _CAP
    assert cb._newest_codex_session(str(tmp_path)) == rollout
    assert opened == [(str(tmp_path), "sessions/2026/09/28/rollout.jsonl")]
    # The rollout vanished between selecting its path and safe-open.
    path.unlink()
    monkeypatch.setattr(cb, "newest_codex_session_path", lambda root: str(path))
    assert cb._newest_codex_session(str(tmp_path)) == ""


def test_a_codex_rollout_that_cannot_be_read_whole_raises__F1669(tmp_path, monkeypatch):
    """Only a gone rollout is empty; failed whole reads refuse export."""
    from collimate import fs
    marker = tmp_path / cb._START_MARKER
    marker.write_text("")
    os.utime(marker, ns=(1, 1))
    path = tmp_path / "sessions/rollout.jsonl"
    path.parent.mkdir()
    path.write_text(_long("codex"))
    def cannot_read(*args, **kwargs):
        raise OSError("cannot read rollout")
    monkeypatch.setattr(fs, "open_file", cannot_read)
    with pytest.raises(OSError, match="cannot read"):
        cb._newest_codex_session(str(tmp_path))


def test_goose_reads_its_session_export_whole__F1669():
    record = '{"messages": [' + ", ".join(f'{{"role": "user", "content": "{i}"}}'
                                         for i in range(200)) + "]}"
    env = _CappedEnv(export="a banner\n" + record)
    assert gb.goose_session_export(env, "20260928_1") == record
    assert env.files == {}      # the export file is removed from the container


def test_a_large_context_file_is_written_back_whole__F1669():
    """A user's AGENTS.md longer than one exec's output keeps every line
    through the inject and the remove."""
    mine = "".join(f"rule {i}: keep it\n" for i in range(200))
    env = _CappedEnv({"/workspace/AGENTS.md": mine})
    cc.inject_context_file(env, "/workspace/AGENTS.md", "Be terse.")
    text = env.files["/workspace/AGENTS.md"]
    assert text.startswith(mine) and "Be terse." in text
    cc.remove_context_file(env, "/workspace/AGENTS.md")
    assert env.files["/workspace/AGENTS.md"] == mine


def test_a_context_file_that_cannot_be_read_whole_is_left_alone__F1669(monkeypatch):
    logs: list = []
    monkeypatch.setattr(cc, "safe_log", logs.append)
    env = _CappedEnv({"/workspace/AGENTS.md": "mine\n"}, unreadable={"/workspace/AGENTS.md"})
    cc.inject_context_file(env, "/workspace/AGENTS.md", "Be terse.")
    assert env.files == {"/workspace/AGENTS.md": "mine\n"}
    assert any("could not read /workspace/AGENTS.md" in line for line in logs)
