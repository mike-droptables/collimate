# SPDX-License-Identifier: MIT
"""Harness context delivery — the two seams.

``cli_container.write_context_seam`` (agent-v2 §9.1) is the PRIMARY one: the
session context goes into the harness's OWN instruction file, in a location
Collimate owns (the container's config dir, or the per-cell harness home), so
nothing about the run ever lands in the user's workspace. Each harness module
names its own path; the seam is auth-independent now that every harness runs
in a per-cell home Collimate owns under every auth.

``inject_context_file`` / ``remove_context_file`` is the FALLBACK, kept for
the harnesses with no such seam (cline, pi) and for the
antigravity terminal leg, whose global rules dir is documented but not yet
verified live: a terminal leg plants its instructions in the CLI's
auto-read workspace file (AGENTS.md, GEMINI.md, .goosehints) WITHOUT replacing
a user's own file of that name, and takes exactly its block out again at
teardown — the cell keeps no harness scaffolding."""
from __future__ import annotations

import shlex


from _agent_shared import cli_container as cc  # noqa: E402


class _FakeEnv:
    """A dict-backed stand-in for the container: whole-file reads through
    read_file and commands through the host-mediated exec plane."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.container = self

    def read_file(self, path, **kw):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path].encode("utf-8")

    def exec(self, argv, **kw):
        cmd = argv[-1]
        if cmd.startswith("rm -f "):
            self.files.pop(shlex.split(cmd)[2], None)
            return 0, ""
        import base64
        b64 = cmd.split("echo ", 1)[1].split(" | base64", 1)[0]
        path = shlex.split(cmd.rsplit("> ", 1)[1])[0]
        self.files[path] = base64.b64decode(shlex.split(b64)[0]).decode("utf-8")

        return 0, ""


def test_inject_then_remove_leaves_no_file_when_it_was_ours():
    env = _FakeEnv()
    cc.inject_context_file(env, "/workspace/AGENTS.md", "Be terse.")
    text = env.files["/workspace/AGENTS.md"]
    assert text.startswith(cc.CONTEXT_BEGIN) and "Be terse." in text
    cc.remove_context_file(env, "/workspace/AGENTS.md")
    assert "/workspace/AGENTS.md" not in env.files


def test_inject_extends_the_users_file_and_remove_restores_it():
    env = _FakeEnv({"/workspace/AGENTS.md": "# My rules\n\nAlways use tabs.\n"})
    cc.inject_context_file(env, "/workspace/AGENTS.md", "Be terse.")
    text = env.files["/workspace/AGENTS.md"]
    assert text.startswith("# My rules\n\nAlways use tabs.\n")
    assert text.count(cc.CONTEXT_BEGIN) == 1
    # Re-injecting replaces our block instead of stacking a second one.
    cc.inject_context_file(env, "/workspace/AGENTS.md", "Be terser.")
    text = env.files["/workspace/AGENTS.md"]
    assert text.count(cc.CONTEXT_BEGIN) == 1 and "Be terser." in text and "Be terse.\n" not in text
    cc.remove_context_file(env, "/workspace/AGENTS.md")
    assert env.files["/workspace/AGENTS.md"] == "# My rules\n\nAlways use tabs.\n"


def test_remove_is_a_noop_without_our_block():
    env = _FakeEnv({"/workspace/AGENTS.md": "theirs\n"})
    cc.remove_context_file(env, "/workspace/AGENTS.md")
    assert env.files["/workspace/AGENTS.md"] == "theirs\n"
    cc.remove_context_file(env, "/workspace/missing.md")


def test_strip_context_block_keeps_content_after_the_block():
    text = f"head\n\n{cc.CONTEXT_BEGIN}\nours\n{cc.CONTEXT_END}\ntail\n"
    assert cc.strip_context_block(text) == "head\ntail\n"


def test_remove_falls_back_to_the_host_when_the_container_is_gone(native_bridge):
    """A terminal leg whose container died still leaves the workspace (the
    host side of the bind mount) — the block is stripped through gen."""
    import gen

    class _DeadEnv:
        def exec(self, argv, **kw):
            raise RuntimeError("404 No such container")

        def read_file(self, path, **kw):
            raise RuntimeError("404 No such container")

    gen.update_file("AGENTS.md", f"mine\n\n{cc.CONTEXT_BEGIN}\nours\n{cc.CONTEXT_END}\n")
    cc.remove_context_file(_DeadEnv(), "/workspace/AGENTS.md")
    assert gen.read_file("AGENTS.md") in ("mine\n", b"mine\n")
    gen.update_file(".goosehints", f"{cc.CONTEXT_BEGIN}\nours\n{cc.CONTEXT_END}\n")
    cc.remove_context_file(_DeadEnv(), "/workspace/.goosehints")
    assert not gen.has_file(".goosehints")


# ── the harness-owned instruction seam (agent-v2 §9.1) ────────────────

def test_write_context_seam_writes_whole_and_clears_on_a_blank_prompt():
    """The seam file is Collimate's alone: written whole and unfenced, and
    REMOVED when the run carries no system prompt — the per-cell harness homes
    outlive the run, so a stale seam would keep instructing the next one."""
    env = _FakeEnv()
    assert cc.write_context_seam(env, "/home/coder/.qwen/QWEN.md", "  Be terse.  ")
    assert env.files["/home/coder/.qwen/QWEN.md"] == "Be terse.\n"
    assert cc.CONTEXT_BEGIN not in env.files["/home/coder/.qwen/QWEN.md"]
    # A rewrite replaces (idempotent on every session reopen).
    cc.write_context_seam(env, "/home/coder/.qwen/QWEN.md", "Be terser.")
    assert env.files["/home/coder/.qwen/QWEN.md"] == "Be terser.\n"
    # A blank prompt clears it rather than leaving last run's text behind.
    assert not cc.write_context_seam(env, "/home/coder/.qwen/QWEN.md", "   ")
    assert "/home/coder/.qwen/QWEN.md" not in env.files


def test_write_context_seam_reports_a_failed_write():
    """A write that exits non-zero is not a seam: the caller must fall back
    (fold / workspace context file), and a stale seam from an earlier run is
    cleared so it cannot keep instructing this one."""
    class _FailingEnv(_FakeEnv):
        def exec(self, argv, **kw):
            if 'base64 -d' in argv[-1]:
                return 1, ''
            return super().exec(argv, **kw)

    env = _FailingEnv({"/home/coder/.qwen/QWEN.md": "old run\n"})
    assert not cc.write_context_seam(env, "/home/coder/.qwen/QWEN.md", "SYS")
    assert "/home/coder/.qwen/QWEN.md" not in env.files


def test_each_harness_seam_is_its_own_instruction_file():
    """One writer per harness, shared by the ACP spawn profile and the TTY
    runner — and none of them is a workspace path."""
    from _agent_shared.harnesses.antigravity import backend as antigravity_backend
    from _agent_shared.harnesses.goose import backend as goose_backend
    from _agent_shared.harnesses.opencode import backend as oc
    from _agent_shared.harnesses.qwen_code import backend as qwen_code_backend

    text = "SYSTEM + skills index + The current date and time is …"

    env = _FakeEnv()
    assert oc.write_context_seam(env, text)
    assert env.files[oc.CONTEXT_SEAM_PATH].startswith("SYSTEM + skills index")
    assert oc.CONTEXT_SEAM_PATH == "/home/coder/.config/opencode/collimate-context.md"

    env = _FakeEnv()
    assert goose_backend.write_context_seam(env, text)
    assert env.files[goose_backend.GOOSE_HINTS_PATH] == text + "\n"
    # goose's per-cell mount is the session store, NOT the config dir.
    assert goose_backend.GOOSE_HINTS_PATH == "/home/coder/.config/goose/.goosehints"

    # Qwen writes the mounted native home before provisioning; its actual
    # preparation seam and blank-prompt clearing are covered by profile tests.

    env = _FakeEnv()
    assert antigravity_backend.write_context_seam(env, text)
    # agy reads GLOBAL rules from <customization root>/rules/*.md, and the
    # per-cell home IS that root — never the workspace, never the user's.
    assert env.files[
        "/home/coder/.gemini/config/rules/collimate-context.md"] == text

    # pydantic-ai, codex, qwen-code, hermes, vibe and copilot read a file in their own
    # home: the pipeline writes it there (harnesses.pipeline.write_instructions).
    from _agent_shared.harnesses import HARNESS
    home_seams = {name: row.instructions.target for name, row in HARNESS.items()
                  if row.instructions.seam == "home_file"}
    assert home_seams == {"codex": "AGENTS.md", "qwen-code": "QWEN.md", "hermes": "SOUL.md",
                          "vibe": "AGENTS.md", "copilot": "copilot-instructions.md",
                          "pi": "agent/collimate-context.md"}

    for path in (oc.CONTEXT_SEAM_PATH, goose_backend.GOOSE_HINTS_PATH,
                 qwen_code_backend.QWEN_CONTEXT_PATH,
                 antigravity_backend.CONTEXT_SEAM_PATH):
        assert not path.startswith("/workspace")


def test_the_seam_goes_to_a_collimate_owned_home_under_every_auth():
    """Every auth runs in the per-cell harness home, so the seam is the same
    file for every auth, and no path points at the user's real home."""
    from _agent_shared.harnesses.antigravity import backend as antigravity_backend
    from _agent_shared.harnesses.codex import backend as codex_backend

    assert codex_backend.CODEX_HOME == "/home/coder/.codex-collimate"

    env = _FakeEnv()
    assert antigravity_backend.write_context_seam(env, "SYS")
    assert env.files == {
        "/home/coder/.gemini/config/rules/collimate-context.md": "SYS"}
