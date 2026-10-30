"""Tests for api/sandbox.py image probes and lazy image builds.

Mocks docker at the ``asyncio.create_subprocess_exec`` layer so the real
sandbox control flow runs without spawning containers.

Collected by normal pytest; Docker is mocked at its subprocess boundary.
"""

from __future__ import annotations
import asyncio
import os
import tempfile


class FakeReader:
    def __init__(self, initial=None):
        self._lines: list[bytes] = []
        for x in initial or []:
            self.push(x)
        self._eof = False
        self._wait = asyncio.Event()
        if self._lines:
            self._wait.set()

    async def readline(self) -> bytes:
        while not self._lines:
            if self._eof:
                return b""
            self._wait.clear()
            await self._wait.wait()
        return self._lines.pop(0)

    async def read(self, n: int = -1) -> bytes:
        while not self._lines and (not self._eof):
            self._wait.clear()
            await self._wait.wait()
        out = b"".join(self._lines)
        self._lines.clear()
        return out

    def push(self, line):
        if isinstance(line, str):
            if not line.endswith("\n"):
                line += "\n"
            line = line.encode("utf-8")
        self._lines.append(line)
        self._wait.set()

    def push_eof(self):
        self._eof = True
        self._wait.set()


class FakeProcess:
    def __init__(self, returncode=0, kind="inspect"):
        self.returncode = returncode
        self.kind = kind
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self._wait_event = asyncio.Event()
        if kind == "inspect":
            self._wait_event.set()
        elif kind == "build":
            self._wait_event.set()

    async def wait(self):
        await self._wait_event.wait()
        return self.returncode

    def kill(self):
        self._wait_event.set()

    def finish(self, returncode=0):
        self.returncode = returncode
        self._wait_event.set()


class FakeDockerHarness:
    """Patches asyncio.create_subprocess_exec to mint FakeProcess instances
    classified by what docker subcommand was invoked. Tests can pre-stage
    per-image inspect statuses."""

    def __init__(self):
        self.image_present: dict[str, bool] = {}
        self.inspect_calls = 0
        self.build_calls = 0
        self.build_argv: list[list[str]] = []
        self._original = None

    def __enter__(self):
        import asyncio as aio

        self._original = aio.create_subprocess_exec

        async def fake(*args, **kwargs):
            argv = list(args)
            if argv and argv[0] == "docker":
                if len(argv) >= 3 and argv[1] == "image" and (argv[2] == "inspect"):
                    self.inspect_calls += 1
                    image = argv[3] if len(argv) > 3 else ""
                    present = self.image_present.get(image, False)
                    proc = FakeProcess(returncode=0 if present else 1, kind="inspect")
                    if not present:
                        proc.stdout.push("Error: No such image: " + image)
                    proc.stdout.push_eof()
                    return proc
                if len(argv) >= 2 and argv[1] == "build":
                    self.build_calls += 1
                    self.build_argv.append(argv)
                    proc = FakeProcess(returncode=0, kind="build")
                    proc.stdout.push("Step 1/N : FROM python:3.12-slim")
                    proc.stdout.push("Successfully built abc123")
                    proc.stdout.push_eof()
                    if "-t" in argv:
                        idx = argv.index("-t")
                        if idx + 1 < len(argv):
                            self.image_present[argv[idx + 1]] = True
                    return proc
            raise AssertionError(f"unexpected Docker command: {argv!r}")

        aio.create_subprocess_exec = fake
        return self

    def __exit__(self, *exc):
        import asyncio as aio

        aio.create_subprocess_exec = self._original


def _reset_sandbox_state():
    import sandbox

    sandbox._image_locks.clear()


def test_probe_image__F1898_F1905():

    async def _run():
        import sandbox

        with FakeDockerHarness() as harness:
            harness.image_present["foo:bar"] = True
            status, _ = await sandbox._probe_docker_image("foo:bar")
            assert status == "present", f"present image returns 'present': got {status}"
            status, _ = await sandbox._probe_docker_image("nope:never")
            assert status == "missing", f"missing image returns 'missing': got {status}"

    asyncio.run(_run())


def test_ensure_image_idempotent__F1898_F1905__P6F01(tmp_path):
    _reset_sandbox_state()

    async def _run():
        import sandbox

        df_dir = tmp_path / "build"
        df_dir.mkdir()
        with open(os.path.join(df_dir, "Dockerfile"), "w") as fh:
            fh.write("FROM scratch\n")
        with FakeDockerHarness() as harness:
            await sandbox.ensure_image("test:img", df_dir, lambda _: None)
            assert harness.inspect_calls == 1, "first call probed"
            assert harness.build_calls == 1, "first call built (image was missing)"
            await sandbox.ensure_image("test:img", df_dir, lambda _: None)
            assert harness.inspect_calls == 2, "second call inspects again"
            assert harness.build_calls == 1, (
                "second call does not rebuild a present image"
            )

    asyncio.run(_run())


def test_build_args_and_reuse__F0227_F0180_F0197__F1898_F1905():
    _reset_sandbox_state()

    async def _run():
        import sandbox

        with tempfile.TemporaryDirectory(prefix="runner_image_") as directory:
            with open(os.path.join(directory, "Dockerfile"), "w") as stream:
                stream.write("ARG PYTHON_VERSION\nFROM python:${PYTHON_VERSION}-slim\n")
            with FakeDockerHarness() as harness:
                args = {"PYTHON_VERSION": "3.14", "SECOND": "value with spaces"}
                await sandbox.ensure_image(
                    "test:args",
                    directory,
                    lambda _: None,
                    refresh=False,
                    build_args=args,
                )
                assert harness.build_argv == [
                    [
                        "docker",
                        "build",
                        "--build-arg",
                        "PYTHON_VERSION=3.14",
                        "--build-arg",
                        "SECOND=value with spaces",
                        "-t",
                        "test:args",
                        directory,
                    ]
                ], "separate deterministic build args and no pull"
                await sandbox.ensure_image(
                    "test:args",
                    directory,
                    lambda _: None,
                    refresh=True,
                    build_args=args,
                )
                assert harness.build_calls == 1, "present same identity reused"

    asyncio.run(_run())


def test_dockerfile_dir_validation__F1898_F1905(tmp_path):
    _reset_sandbox_state()

    async def _run():
        import sandbox

        try:
            await sandbox._build_image("x:y", "/no/such/dir/ever", lambda _: None)
            assert False, "missing dir raises: no exception raised"
        except RuntimeError as exc:
            assert "does not exist" in str(exc), (
                f"missing dir raises with actionable message: {str(exc)}"
            )
        empty = tmp_path / "empty"
        empty.mkdir()
        try:
            await sandbox._build_image("x:y", empty, lambda _: None)
            assert False, "missing Dockerfile raises"
        except RuntimeError as exc:
            assert "no Dockerfile" in str(exc), (
                f"missing Dockerfile raises with actionable message: {str(exc)}"
            )

    asyncio.run(_run())
