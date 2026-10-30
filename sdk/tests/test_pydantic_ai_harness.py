# SPDX-License-Identifier: MIT
"""The pydantic-ai harness: Pydantic AI's own ACP adapter as a host process.

``harnesses/pydantic_ai/backend.py`` builds the spawn profile (never importing
``server.py``, which runs in its own environment), and
``host_adapters.ensure_python_adapter`` installs the pinned environment that
server runs in.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from _agent_shared import adapter_artifacts, host_adapters  # noqa: E402
from _agent_shared.harnesses import HARNESS, pipeline  # noqa: E402
from _agent_shared.harnesses.pydantic_ai import backend  # noqa: E402
from tests._commands_matrix import recording_runtime  # noqa: E402, F401

ENV = {"wire": "openai", "endpoint": "https://models.example/v1", "model": "m",
       "key_fd": 7, "provider": "openai", "commands": "ask",
       "file_tools": True, "private": "/run/p"}


# ── model_wire ────────────────────────────────────────────────────────


@pytest.mark.parametrize("protocol,wire", [
    ("openai", "openai"), ("openai-compatible", "openai"), ("openai-chat", "openai"),
    ("openai-responses", "openai-responses"), ("anthropic", "anthropic"), ("gemini", "gemini"),
])
def test_model_wire_follows_the_protocol_never_the_provider(protocol, wire):
    # An OpenRouter, Groq or Cerebras account is OpenAI-wire by its protocol.
    assert backend.model_wire({"service": "groq", "protocol": protocol}) == wire


@pytest.mark.parametrize("account", [{"protocol": "cli"}, {"protocol": "unrecognized"}, {}])
def test_model_wire_refuses_a_protocol_it_cannot_speak(account):
    with pytest.raises(ValueError, match="Unsupported model connection protocol"):
        backend.model_wire(account)


# ── server_env ────────────────────────────────────────────────────────


def test_server_env_is_the_servers_whole_configuration():
    env = backend.server_env(**ENV)
    assert env == {
        "COLLIMATE_PAI_SANDBOX": "",
        "COLLIMATE_PAI_WIRE": "openai",
        "COLLIMATE_PAI_ENDPOINT": "https://models.example/v1",
        "COLLIMATE_PAI_MODEL": "m",
        "COLLIMATE_PAI_KEY_FD": "7",
        "COLLIMATE_PAI_STRICT_TOOLS": "1",
        "COLLIMATE_PAI_COMMANDS": "ask",
        "COLLIMATE_PAI_FILE_TOOLS": "1",
        "COLLIMATE_PAI_INSTRUCTIONS": "/run/p/instructions.md",
        "COLLIMATE_PAI_SESSIONS": "/run/p/sessions",
        "COLLIMATE_PAI_VERSION": json.dumps(adapter_artifacts.PYTHON_ADAPTERS["pydantic-ai-harness"].version),
    }
    # The key travels as a file path, never as a value.
    assert not [name for name in env if name.endswith(("KEY", "TOKEN", "SECRET"))]


def test_server_env_turns_strict_tool_schemas_off_for_cerebras_only():
    assert backend.server_env(**{**ENV, "provider": "cerebras"})["COLLIMATE_PAI_STRICT_TOOLS"] == "0"
    for provider in ("openai", "openrouter", "groq", "anthropic", "gemini"):
        assert backend.server_env(**{**ENV, "provider": provider})["COLLIMATE_PAI_STRICT_TOOLS"] == "1"


def test_server_env_carries_the_file_tools_and_commands_axes():
    off = backend.server_env(**{**ENV, "file_tools": False, "commands": "deny"})
    assert off["COLLIMATE_PAI_FILE_TOOLS"] == "0" and off["COLLIMATE_PAI_COMMANDS"] == "deny"


# ── the adapter declaration ───────────────────────────────────────────


def test_server_env_names_the_sandbox_and_the_daemon_only_with_one(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    assert "DOCKER_HOST" not in backend.server_env(**ENV)
    env = backend.server_env(**{**ENV, "sandbox": "c0ffee"})
    assert env["COLLIMATE_PAI_SANDBOX"] == "c0ffee"
    assert env["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"


def test_the_adapter_is_a_host_acp_harness_whose_shell_is_sandboxed():
    adapter = HARNESS["pydantic-ai"]
    # The agent is a process here; its image is the command sandbox only.
    assert adapter.locus == "host" and adapter.terminal is None and adapter.image == "image"
    assert adapter.commands.sandboxed and "recommended for commands: allow" in adapter.note
    # The instruction file and sessions live in a home outside the workspace;
    # the cell keeps the sessions between runs.
    assert adapter.instructions.seam == "meta" and adapter.instructions.target == "COLLIMATE_PAI_INSTRUCTIONS"
    assert adapter.session.paths == ("sessions",)
    assert adapter.tools.web == "none"
    assert adapter.prepare == "harnesses.pydantic_ai.backend:prepare"
    assert adapter.label == "Pydantic AI" and adapter.auth.custom_endpoint
    assert "pydantic-ai-harness" in adapter_artifacts.PYTHON_ADAPTERS


# ── prepare ───────────────────────────────────────────────────────────


def _ctx(**changes):
    return {"auth": "api_key", "provider": "openrouter", "model": "openai/gpt-4.1-mini",
            "system_prompt": "be brief", "enable_file_tools": True, "websearch": False,
            "commands": "ask", **changes}


def test_prepare_builds_a_host_profile_with_the_key_only_on_a_pipe(recording_runtime):  # noqa: F811
    env = recording_runtime
    prepared = asyncio.run(pipeline.prepare(HARNESS["pydantic-ai"], _ctx()))
    profile = prepared.profile
    assert profile.locus == "host" and profile.commands == "ask"
    assert profile.bind_host == "127.0.0.1"
    assert profile.model_applied and profile.model_value == "openai/gpt-4.1-mini"
    assert profile.adapter_version == adapter_artifacts.PYTHON_ADAPTERS["pydantic-ai-harness"].version
    assert prepared.commands.level == "ask"
    asyncio.run(profile.spawn())
    assert env.spawn["backend"] == "pydantic-ai"
    assert env.spawn["argv"] == ["/fake/pydantic-ai/python", "-I", backend.SERVER]
    variables = env.spawn["env"]
    assert variables["COLLIMATE_PAI_WIRE"] == "openai"
    assert variables["COLLIMATE_PAI_ENDPOINT"] == "https://openrouter.ai/api/v1"
    assert variables["COLLIMATE_PAI_COMMANDS"] == "ask"
    # The key reaches the server on an inherited pipe only: in no env value
    # and on no disk.
    assert env.spawn["pass_fds"] == (int(variables["COLLIMATE_PAI_KEY_FD"]),)
    assert list(env.spawn["inherited"].values()) == ["matrix-fake-key"]
    assert "matrix-fake-key" not in json.dumps(variables)
    assert "matrix-fake-key" not in os.environ.values()
    # The instruction file and the session store are in the harness home,
    # outside the workspace the sandbox mounts.
    private = Path(variables["COLLIMATE_PAI_INSTRUCTIONS"]).parent
    assert (private / "instructions.md").read_text().strip() == "be brief"
    assert variables["COLLIMATE_PAI_SESSIONS"] == str(private / "sessions")
    assert stat.S_IMODE(os.stat(private).st_mode) == 0o700
    import gen
    workspace = Path(gen.workspace_path()).resolve()
    assert workspace not in private.resolve().parents
    assert not prepared.profile.fold_system_prompt


def test_nothing_under_the_sandbox_mount_holds_the_key_or_the_instructions(recording_runtime):  # noqa: F811
    """The docker: full sandbox bind-mounts the run workspace: no secret and
    no server file may be under it, whatever the leg wrote."""
    import gen
    from collimate import native
    env = recording_runtime
    native.current_bridge().permissions = {"docker": "full", "internet": "full", "commands": "allow"}
    prepared = asyncio.run(pipeline.prepare(HARNESS["pydantic-ai"], _ctx(commands="allow")))
    asyncio.run(prepared.profile.spawn())
    asyncio.run(prepared.profile.spawn())
    assert env.spawn["env"]["COLLIMATE_PAI_SANDBOX"]
    for path in Path(gen.workspace_path()).rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            assert b"matrix-fake-key" not in body, path
            assert b"be brief" not in body, path


def test_the_sessions_live_outside_the_workspace_and_carry_into_the_next_run(recording_runtime):  # noqa: F811
    """A sandboxed command never reaches the store the host server reads and
    writes, and the cell still keeps it: a later run's home is seeded with
    the session a run saved."""
    import gen
    from _agent_shared import harness_homes
    workspace = Path(gen.workspace_path()).resolve()
    home = Path(backend.private_dir())
    assert workspace not in home.resolve().parents
    assert harness_homes._cache_root() in str(home)
    (home / "sessions").mkdir()
    (home / "sessions" / "s1.json").write_text('{"messages": []}')
    harness_homes.close_harness_homes()
    later = Path(backend.private_dir())
    assert later != home and (later / "sessions" / "s1.json").read_text() == '{"messages": []}'
    # A harness whose commands share its own process keeps its home in the
    # workspace, as before.
    assert workspace in Path(harness_homes.harness_home("codex")).resolve().parents


def test_every_spawn_gets_a_fresh_key_pipe(recording_runtime):  # noqa: F811
    env = recording_runtime
    profile = asyncio.run(pipeline.prepare(HARNESS["pydantic-ai"], _ctx())).profile
    asyncio.run(profile.spawn())
    first = env.spawn["inherited"]
    asyncio.run(profile.spawn())
    second = env.spawn["inherited"]
    assert list(first.values()) == list(second.values()) == ["matrix-fake-key"]


def test_prepare_takes_the_accounts_custom_endpoint_and_wire(recording_runtime, monkeypatch):  # noqa: F811
    from _agent_shared import accounts
    env = recording_runtime
    monkeypatch.setattr(accounts, "model_account", lambda: {
        "kind": "model", "service": "custom", "protocol": "anthropic",
        "endpoint": "https://proxy.example/v1/", "model": "claude-on-proxy"})
    profile = asyncio.run(pipeline.prepare(
        HARNESS["pydantic-ai"], _ctx(provider="anthropic", model="claude-on-proxy"))).profile
    asyncio.run(profile.spawn())
    variables = env.spawn["env"]
    assert variables["COLLIMATE_PAI_WIRE"] == "anthropic"
    assert variables["COLLIMATE_PAI_ENDPOINT"] == "https://proxy.example/v1"
    assert variables["COLLIMATE_PAI_MODEL"] == "claude-on-proxy"


def test_prepare_refuses_an_account_protocol_the_server_cannot_speak(recording_runtime, monkeypatch):  # noqa: F811
    from _agent_shared import accounts
    monkeypatch.setattr(accounts, "model_account", lambda: {
        "kind": "model", "service": "openrouter", "protocol": "unrecognized", "model": "m"})
    profile = asyncio.run(pipeline.prepare(HARNESS["pydantic-ai"], _ctx())).profile
    with pytest.raises(ValueError, match="Unsupported model connection protocol"):
        asyncio.run(profile.spawn())


def test_prepare_refuses_the_terminal_leg():
    ctx = SimpleNamespace()
    with pytest.raises(ValueError, match="no terminal"):
        asyncio.run(backend.prepare(ctx, leg="terminal"))


# ── ensure_python_adapter ─────────────────────────────────────────────

NAME = "pydantic-ai-harness"
PINNED = adapter_artifacts.PYTHON_ADAPTERS[NAME].version


class FakeEnvironments:
    """``subprocess.run`` for the installer and the preflight: venvs are
    directories holding a ``python`` file and a recorded version."""

    def __init__(self, monkeypatch, tmp_path, *, uv=True, install_version=None, fail_install=False):
        self.calls: list = []
        self.installs = 0
        self.versions: dict = {}
        self.install_version = install_version
        self.fail_install = fail_install
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        host_adapters._PREFLIGHTED.clear()
        monkeypatch.setattr(host_adapters.shutil, "which", lambda name: "/usr/bin/uv" if uv and name == "uv" else None)
        monkeypatch.setattr(host_adapters.subprocess, "run", self.run)

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        done = SimpleNamespace(returncode=0, stdout="", stderr="")
        if "-c" in argv:
            return SimpleNamespace(returncode=0, stdout=self.versions.get(argv[0], "") + "\n", stderr="")
        if "venv" in argv:
            venv = argv[-1]
            os.makedirs(os.path.join(venv, "bin"))
            Path(venv, "bin", "python").write_text("")
            return done
        if "install" in argv:
            self.installs += 1
            if self.fail_install:
                return SimpleNamespace(returncode=1, stdout="", stderr="no matching distribution")
            python = argv[argv.index("--python") + 1] if "--python" in argv else argv[0]
            self.versions[python] = self.install_version or PINNED
            self.requirements = argv[argv.index("install") + 1:]
            return done
        raise AssertionError(f"unexpected command {argv}")


def test_the_python_adapter_is_pinned_exact():
    artifact = adapter_artifacts.PYTHON_ADAPTERS[NAME]
    assert artifact.module == NAME and artifact.version == PINNED
    assert all("==" in requirement for requirement in artifact.requirements)
    assert any(requirement.endswith(f"=={PINNED}") for requirement in artifact.requirements)


def test_ensure_python_adapter_installs_once_and_reuses_it(monkeypatch, tmp_path):
    fake = FakeEnvironments(monkeypatch, tmp_path)
    python = host_adapters.ensure_python_adapter(NAME)
    assert python == host_adapters.python_adapter_interpreter(NAME)
    assert python.startswith(str(tmp_path / "cache" / "collimate" / "acp-adapters" / NAME / PINNED))
    assert os.path.exists(python) and fake.installs == 1
    assert [step[1] for step in fake.calls if step[0] == "/usr/bin/uv"] == ["venv", "pip"]
    assert all(requirement in fake.requirements for requirement in adapter_artifacts.PYTHON_ADAPTERS[NAME].requirements)
    # The same process asks again: no subprocess at all.
    before = len(fake.calls)
    assert host_adapters.ensure_python_adapter(NAME) == python
    assert len(fake.calls) == before
    # A later process (nothing remembered) preflights and reuses it, never reinstalling.
    host_adapters._PREFLIGHTED.clear()
    assert host_adapters.ensure_python_adapter(NAME) == python
    assert fake.installs == 1 and len(fake.calls) == before + 1


def test_ensure_python_adapter_installs_with_venv_and_pip_without_uv(monkeypatch, tmp_path):
    fake = FakeEnvironments(monkeypatch, tmp_path, uv=False)
    python = host_adapters.ensure_python_adapter(NAME)
    assert os.path.exists(python)
    assert fake.calls[0][1:3] == ["-m", "venv"]
    assert fake.calls[1][:4] == [python, "-m", "pip", "install"]


def test_a_wrong_version_is_reinstalled(monkeypatch, tmp_path):
    fake = FakeEnvironments(monkeypatch, tmp_path)
    python = host_adapters.python_adapter_interpreter(NAME)
    os.makedirs(os.path.dirname(python))
    Path(python).write_text("")
    fake.versions[python] = "0.0.1"
    assert host_adapters.ensure_python_adapter(NAME) == python
    assert fake.installs == 1 and fake.versions[python] == PINNED


def test_a_failed_install_raises_a_named_error(monkeypatch, tmp_path):
    FakeEnvironments(monkeypatch, tmp_path, fail_install=True)
    with pytest.raises(RuntimeError, match="installing the pydantic-ai-harness adapter failed: no matching distribution"):
        host_adapters.ensure_python_adapter(NAME)
    assert not host_adapters._PREFLIGHTED


def test_an_install_that_still_has_the_wrong_version_raises_a_named_error(monkeypatch, tmp_path):
    FakeEnvironments(monkeypatch, tmp_path, install_version="9.9.9")
    with pytest.raises(RuntimeError, match=r"pydantic-ai-harness adapter environment .* failed after a fresh install: "
                                           r"it holds pydantic-ai-harness 9.9.9, expected " + PINNED.replace(".", r"\.")):
        host_adapters.ensure_python_adapter(NAME)



# ── the commands level is the gate; the docker grant picks where ──────


@pytest.mark.parametrize("level,shell,delegation", [("deny", False, False), ("ask", True, False),
                                                    ("allow", True, True)])
def test_the_commands_level_is_the_gate(level, shell, delegation):
    from _agent_shared.harnesses.pydantic_ai import policy
    assert policy.shell_offered(level) is shell
    assert policy.delegation_offered(level) is delegation
    # The docker grant picks only where the shell runs.
    assert policy.wants_sandbox(level, "full") is shell
    for docker in ("none", "limited"):
        assert policy.wants_sandbox(level, docker) is False
        assert bool(policy.host_shell_note(level, docker)) is shell
    assert policy.host_shell_note(level, "full") == ""


def _grant(docker):
    from collimate import native
    native.current_bridge().permissions = {"docker": docker, "internet": "full", "commands": "allow"}


@pytest.mark.parametrize("level", ["deny", "ask", "allow"])
def test_under_docker_none_the_shell_runs_on_this_machine(recording_runtime, level):  # noqa: F811
    env = recording_runtime
    _grant("none")
    prepared = asyncio.run(pipeline.prepare(HARNESS["pydantic-ai"], _ctx(commands=level)))
    assert prepared.profile.commands == level
    assert bool(prepared.profile.commands_notice) is (level != "deny")
    if level != "deny":
        assert "docker: full sandboxes them" in prepared.profile.commands_notice
    # No sandbox is placed: Coder's workspace is the run directory here.
    assert not [name for name, _kw in env.ensures if name == "pydantic-ai"]
    asyncio.run(prepared.profile.spawn())
    variables = env.spawn["env"]
    assert variables["COLLIMATE_PAI_COMMANDS"] == level and variables["COLLIMATE_PAI_SANDBOX"] == ""


@pytest.mark.parametrize("level,sandboxed", [("deny", False), ("ask", True), ("allow", True)])
def test_under_docker_full_the_shell_is_the_sandbox_containers(recording_runtime, level, sandboxed):  # noqa: F811
    env = recording_runtime
    _grant("full")
    prepared = asyncio.run(pipeline.prepare(HARNESS["pydantic-ai"], _ctx(commands=level)))
    assert prepared.profile.commands == level and not prepared.profile.commands_notice
    # The host places the sandbox from the harness's own image context, the
    # workspace mounted as for every harness container — only for a shell.
    placed = [kw for name, kw in env.ensures if name == "pydantic-ai"]
    assert placed == ([{"build_dir": "pydantic-ai"}] if sandboxed else [])
    asyncio.run(prepared.profile.spawn())
    variables = env.spawn["env"]
    assert variables["COLLIMATE_PAI_COMMANDS"] == level
    assert variables["COLLIMATE_PAI_SANDBOX"] == (env.container_id if sandboxed else "")


def test_the_sandbox_image_carries_the_workspace_label_and_a_writable_pid_dir():
    dockerfile = (Path(backend.__file__).parent / "image" / "Dockerfile").read_text()
    assert "LABEL ai.pydantic.workspace=true" in dockerfile
    assert "/run/pydantic-ai" in dockerfile and "USER coder" in dockerfile


def test_the_harness_metadata_dir_is_never_run_output():
    from _agent_shared.workspace_tools import not_restaged
    assert not_restaged(".pydantic-ai-harness/shell/abc/output.log")
    assert not_restaged(".pydantic-ai-harness") and not not_restaged("pydantic-ai-harness.md")
