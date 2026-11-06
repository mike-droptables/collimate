"""The uv and the Python a native generator run starts with
(``process_manager.native_tools``).

Pinned:
  * ``--server-env ROOT`` (the desktop package's server) starts every native
    run with ``ROOT/bin/uv`` and passes ``--python ROOT/bin/python3`` when the
    entrypoint's PEP 723 ``requires-python`` admits that interpreter, saying
    so in the run log; a ``requires-python`` that excludes it, or one the host
    cannot read, gets no ``--python`` (uv's discovery chooses, and may
    download) and a log line saying that instead of a refused run;
  * without it (a checkout, ``uvx collimate``) a native run is
    ``uv run <entrypoint>`` with nothing chosen and nothing logged;
  * a container run's tail keeps its image's uv and Python either way;
  * the CLI resolves ``--server-env`` before its chdir and refuses an
    environment without an executable ``bin/uv`` and ``bin/python3`` that
    answers its version.
"""
from __future__ import annotations

import asyncio

import pytest


def _tools():
    """The native_tools module the server reads now: the api suite's
    conftest re-imports server modules between tests, so a reference taken
    at import time could be a module the spawn and the CLI no longer use."""
    import collimate_server.process_manager.native_tools as tools
    return tools


def admits(specifier, version):
    return _tools().admits(specifier, version)


def requires_python(entrypoint):
    return _tools().requires_python(entrypoint)


def run_python(entrypoint):
    return _tools().run_python(entrypoint)


def _environment(root, *, version="3.13.18"):
    """A fake server environment: a ``bin/uv`` and a ``bin/python3`` that
    answers ``version``."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "python3").write_text(f"#!/bin/sh\necho {version}\n")
    for name in ("uv", "python3"):
        (bin_dir / name).chmod(0o755)
    return root


@pytest.fixture
def server_env(tmp_path):
    """configure_server_env on a fake environment for one test, and none after it."""
    root = _environment(tmp_path / "server")
    yield _tools().configure_server_env(str(root))
    _tools().configure_server_env(None)


def _entrypoint(folder, requires=None, *, block=True):
    header = ""
    if block:
        lines = ["# /// script"]
        if requires is not None:
            lines.append(f'# requires-python = "{requires}"')
        lines += ["# dependencies = []", "# ///", ""]
        header = "\n".join(lines)
    (folder / "main.py").write_text(header + "import gen\n")
    return str(folder / "main.py")


# ===========================================================================
# requires-python, read and checked
# ===========================================================================

def test_requires_python_reads_the_script_block(tmp_path):
    assert requires_python(_entrypoint(tmp_path, ">=3.12")) == ">=3.12"
    assert requires_python(_entrypoint(tmp_path, None)) is None
    assert requires_python(_entrypoint(tmp_path, block=False)) is None
    assert requires_python(str(tmp_path / "missing.py")) is None
    two = tmp_path / "two.py"
    two.write_text('# /// script\n# requires-python = ">=3.12"\n# ///\n'
                   '# /// script\n# requires-python = ">=3.14"\n# ///\n')
    assert requires_python(str(two)) is None


@pytest.mark.parametrize("specifier, admitted", [
    (">=3.12", True), (">=3.13", True), (">=3.13.18", True), (">=3.13.19", False), (">=3.14", False),
    (">3.13", True), (">3.13.18", False), ("<3.14", True), ("<3.13", False), ("<=3.13.18", True),
    ("==3.13.*", True), ("==3.12.*", False), ("!=3.13.*", False), ("!=3.12.*", True),
    ("==3.13.18", True), ("==3.13", False), ("!=3.13.18", False), ("~=3.13", True), ("~=3.12", True),
    ("~=3.13.20", False), ("~=3.13.2", True), ("~=3.12.0", False), (">=3.10, <3.14", True),
    (">=3.10,<3.13", False), ("===3.13.18", True), ("===3.13", False),
])
def test_admits_reads_release_specifiers(specifier, admitted):
    assert admits(specifier, "3.13.18") is admitted


@pytest.mark.parametrize("specifier", [">=3.14.0a1", ">=3.13.post1", "~=3", ">=3.12,", "3.12", ">= x", ""])
def test_admits_says_nothing_of_what_it_does_not_read(specifier):
    assert admits(specifier, "3.13.18") is None


# ===========================================================================
# The interpreter a run gets
# ===========================================================================

def test_without_a_server_env_nothing_is_chosen(tmp_path):
    assert _tools().server_env() is None
    assert _tools().native_uv() == "uv"
    assert run_python(_entrypoint(tmp_path, ">=3.12")) == (None, None)


def test_the_server_environments_python_where_requires_python_admits_it(tmp_path, server_env):
    for requires in (">=3.12", None):
        python, said = run_python(_entrypoint(tmp_path, requires))
        assert python == server_env.python
        assert said == f"Python 3.13.18, the server environment's ({server_env.python})"
    assert run_python(_entrypoint(tmp_path, block=False))[0] == server_env.python


def test_uv_chooses_where_requires_python_excludes_it_or_cannot_be_checked(tmp_path, server_env):
    python, said = run_python(_entrypoint(tmp_path, ">=3.14"))
    assert python is None
    assert said == ("requires-python '>=3.14' excludes the server environment's Python 3.13.18: "
                    "uv chooses this run's Python (and downloads one if none on this machine fits)")
    python, said = run_python(_entrypoint(tmp_path, ">=3.14.0rc1"))
    assert python is None
    assert said.startswith("requires-python '>=3.14.0rc1' is more than this server checks against its "
                           "Python 3.13.18: uv chooses")


# ===========================================================================
# The native spawn and the container tail
# ===========================================================================

class _Manager:
    def __init__(self, app_dir, requirements=()):
        from collimate_server.runner_permissions import RunnerPermissions
        self.app_dir = str(app_dir)
        self.optional_requirements = list(requirements)
        self.permissions = RunnerPermissions.from_definition(
            {"docker": "none", "internet": "full", "update_packages": True})
        self.logged: list[str] = []

    def push_log(self, line):
        self.logged.append(line)


async def _spawn(monkeypatch, manager, entrypoint="main.py"):
    from collimate_server.process_manager.backends import NativeSubprocessBackend
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await NativeSubprocessBackend().spawn(manager, env={}, entrypoint=entrypoint, manifest={}, preexec=None)
    return captured["argv"]


async def test_a_server_env_starts_a_native_run_with_its_uv_on_its_python(tmp_path, monkeypatch, server_env):
    _entrypoint(tmp_path, ">=3.12")
    manager = _Manager(tmp_path, ["engine-b"])
    assert await _spawn(monkeypatch, manager) == [
        server_env.uv, "run", "--python", server_env.python, "--with", "engine-b",
        "--upgrade-package", "engine-b", "main.py"]
    assert manager.logged == [f"Python 3.13.18, the server environment's ({server_env.python})"]


async def test_a_run_whose_requires_python_excludes_it_runs_on_uvs_choice(tmp_path, monkeypatch, server_env):
    _entrypoint(tmp_path, ">=3.14")
    manager = _Manager(tmp_path)
    assert await _spawn(monkeypatch, manager) == [server_env.uv, "run", "main.py"]
    assert len(manager.logged) == 1 and "excludes the server environment's Python 3.13.18" in manager.logged[0]


async def test_without_a_server_env_a_native_run_is_unchanged(tmp_path, monkeypatch):
    _entrypoint(tmp_path, ">=3.12")
    manager = _Manager(tmp_path)
    assert await _spawn(monkeypatch, manager) == ["uv", "run", "main.py"]
    assert manager.logged == []


def test_a_container_run_keeps_its_images_uv_and_python(server_env):
    from collimate_server.process_manager.docker_runner import RunnerSpec, build_run_argv
    argv = build_run_argv(RunnerSpec(
        image="collimate-runner:py3.13", container_name="collimate-run-RID", run_id="RID",
        app_dir="/home/u/a", workspace_dir="/home/u/w", sdk_src="/home/u/s", entrypoint="main.py",
        env={}, network="bridge"))
    assert argv[-4:] == ["collimate-runner:py3.13", "uv", "run", "main.py"]
    assert server_env.python not in argv and "--python" not in argv


# ===========================================================================
# The CLI
# ===========================================================================

def _cli(monkeypatch, argv):
    import sys

    import uvicorn

    import collimate_server.main as main
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["collimate", *argv])
    monkeypatch.delenv("COLLIMATE_TOKEN", raising=False)
    main.cli_main()


@pytest.fixture
def unconfigured():
    yield
    _tools().configure_server_env(None)


def test_the_cli_names_the_server_env_resolved_before_the_chdir(tmp_path, monkeypatch, unconfigured):
    root = _environment(tmp_path / "server", version="3.13.18")
    monkeypatch.chdir(tmp_path)
    _cli(monkeypatch, ["--data-dir", str(tmp_path / "data"), "--server-env", "server"])
    env = _tools().server_env()
    assert env == _tools().ServerEnv(root=str(root), uv=str(root / "bin" / "uv"),
                                         python=str(root / "bin" / "python3"), version="3.13.18")
    assert _tools().native_uv() == str(root / "bin" / "uv")
    _tools().configure_server_env(None)
    _cli(monkeypatch, ["--data-dir", str(tmp_path / "data")])
    assert _tools().server_env() is None and _tools().native_uv() == "uv"


@pytest.mark.parametrize("breakage, reason", [
    ("no-uv", "it has no executable bin/uv"),
    ("plain-python", "it has no executable bin/python3"),
    ("folder-uv", "it has no executable bin/uv"),
    ("failing-python", "its bin/python3 does not run"),
    ("odd-python", "its bin/python3 says its version is 'Python 3'"),
])
def test_the_cli_refuses_a_server_env_it_cannot_use(tmp_path, monkeypatch, capsys, unconfigured, breakage, reason):
    root = _environment(tmp_path / "server")
    uv, python = root / "bin" / "uv", root / "bin" / "python3"
    if breakage == "no-uv":
        uv.unlink()
    elif breakage == "folder-uv":
        uv.unlink()
        uv.mkdir()
    elif breakage == "plain-python":
        python.chmod(0o644)
    elif breakage == "failing-python":
        python.write_text("#!/bin/sh\nexit 3\n")
    elif breakage == "odd-python":
        python.write_text("#!/bin/sh\necho 'Python 3'\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        _cli(monkeypatch, ["--data-dir", str(tmp_path / "data"), "--server-env", str(root)])
    assert exit_info.value.code == 2
    assert f"--server-env {root}: {reason}" in capsys.readouterr().err
    assert _tools().server_env() is None and not (tmp_path / "data").exists()
