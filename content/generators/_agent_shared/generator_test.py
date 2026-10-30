# SPDX-License-Identifier: MIT
"""Run a draft through the parent's existing command boundary and fixed IPC relay."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import stat
import subprocess

import gen
from collimate.testing.forward import Forwarder, prepare_child

TIMEOUT_S = 120.0
OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class CommandContext:
    """An explicit command placement; never inferred from a global harness."""

    locus: str
    env_obj: object | None = None

    def __post_init__(self):
        if self.locus not in ('host', 'container'):
            raise ValueError('test_generator needs a host or container command context')
        if self.locus == 'host' and self.env_obj is not None:
            raise ValueError('host command context cannot contain a container')
        if self.locus == 'container' and self.env_obj is None:
            raise ValueError('container command context needs its environment')


def docker_gateway():
    from .mcp_bridge import _docker_gateway_host
    return _docker_gateway_host()


def entries(*, commands: str, context: CommandContext | None) -> list:
    if commands not in ('ask', 'allow') or context is None:
        return []

    async def test_generator(path: str, settings: dict | None = None, prompt: str = '') -> dict:
        """Test a draft .generator in this agent's command environment.

        Args:
            path: Cell-relative packaged .generator definition.
            settings: Values overlaid on the draft's main config.
            prompt: Prompt supplied only to this test run.

        The child shares this cell and can publish writes immediately. It gets
        this run's identity and grants; accounts, environments and UI/session
        operations are unavailable. Add the generator to a real runner to test
        those capabilities. Stopping ends future relay calls; committed writes
        remain. Output is bounded and execution times out after two minutes.
        """
        return await run_test(context, commands=commands, path=path,
                              settings=settings, prompt=prompt)

    return [('test_generator', 'command', True, 'confined uv child', test_generator)]


async def run_test(context: CommandContext, *, commands: str, path: str,
                   settings: dict | None, prompt: str) -> dict:
    if commands not in ('ask', 'allow'):
        return {'error': 'commands_denied'}
    if commands == 'ask':
        from .approval import approve_command
        detail = json.dumps({'path': path, 'settings': settings, 'prompt': prompt},
                            ensure_ascii=False, indent=2)
        refused = await approve_command('test_generator', detail)
        if refused:
            return {'error': 'approval_refused', 'message': refused}
    try:
        return await _execute(context, path=path, settings=settings, prompt=prompt)
    except Exception as exc:
        return {'error': 'test_generator_failed', 'message': str(exc)}


def _host_environment(child) -> dict:
    # Do not inherit the parent's IPC token, account variables or private config.
    env = {name: os.environ[name] for name in (
        'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT', 'WINDIR', 'UV_CACHE_DIR',
    ) if name in os.environ}
    env.update(child.env)
    return env


async def _stop_host(process):
    if os.name == 'nt':
        if process.returncode is not None:
            return
        # Windows has no killpg. taskkill /T terminates uv and its descendants;
        # process.kill is a final fallback if the system utility is unavailable.
        killer = None
        try:
            killer = await asyncio.create_subprocess_exec(
                os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32', 'taskkill.exe'),
                '/PID', str(process.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(killer.wait(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            if killer is not None and killer.returncode is None:
                killer.kill()
                await killer.wait()
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    finally:
        # The uv parent may exit before a descendant that ignored TERM.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


async def _host_command(child):
    group = ({'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)}
             if os.name == 'nt' else {'start_new_session': True})
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *child.argv, cwd=child.cwd, env=_host_environment(child),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, **group))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await spawn
        await _stop_host(process)
        raise
    output = bytearray()
    truncated = False

    async def read():
        nonlocal truncated
        while block := await process.stdout.read(8192):
            output.extend(block)
            if len(output) > OUTPUT_BYTES:
                del output[:-OUTPUT_BYTES]
                truncated = True

    reader = asyncio.create_task(read())
    try:
        code = await process.wait()
        await reader
        text = output.decode('utf-8', 'replace')
        return code, ('[earlier output truncated]\n' if truncated else '') + text
    finally:
        await _stop_host(process)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


# The supervisor is ordinary command-environment code, never draft code in the
# parent. Docker owns and reaps the detached exec; env_exec ACKs after launch.
_CONTAINER_SUPERVISOR = r"""
import ctypes, json, os, selectors, signal, subprocess, sys, time
from pathlib import Path
root, limit, duration, argv = Path(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), json.loads(sys.argv[4])
if os.getpgrp() != os.getpid():
    os.setsid()
os.umask(0o077)
with open(os.devnull, 'r+b', buffering=0) as null:
    for descriptor in (0, 1, 2):
        os.dup2(null.fileno(), descriptor)
# Adopt grandchildren when their immediate parent exits, so a container with
# sleep as PID 1 never receives an unreaped orphan from this command tree.
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    raise OSError(ctypes.get_errno(), 'cannot establish command child subreaper')
tail = bytearray()
truncated = False
stopping = False
process = None

def request_stop(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, request_stop)

def publish(name, value):
    path = root / (name + '.tmp')
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle)
    path.replace(root / name)

def cancelled():
    return stopping or not root.is_dir() or (root / 'command.cancel').exists()

def reap_tree():
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    children = Path('/proc/self/task') / str(os.getpid()) / 'children'
    while True:
        # A descendant may have started its own session. Adoption still makes
        # it our child; kill only these kernel-reported owned child PIDs.
        for value in children.read_text().split():
            try:
                os.kill(int(value), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while True:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                if process is not None and pid == process.pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.01)

def execute():
    global process, truncated
    pid = os.getpid()
    start = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]
    publish('command.ready', {'pid': pid, 'start': start})
    launch_deadline = time.monotonic() + 10
    while not (root / 'command.start').exists():
        if cancelled() or time.monotonic() >= launch_deadline:
            return 130, ''
        time.sleep(.02)
    if cancelled():
        return 130, ''
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + duration
    exited_at = None
    eof = False
    try:
        while True:
            if cancelled():
                return 130, None
            if time.monotonic() >= deadline:
                return 124, None
            for key, _ in selector.select(.02):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    eof = True
                    continue
                tail.extend(chunk)
                if len(tail) > limit:
                    del tail[:-limit]
                    truncated = True
            code = process.poll()
            if code is not None:
                exited_at = exited_at or time.monotonic()
                if eof or time.monotonic() - exited_at >= .2:
                    return code, None
    finally:
        selector.close()
        process.stdout.close()

try:
    code, text = execute()
except Exception as exc:
    code, text = 127, type(exc).__name__ + ': ' + str(exc)
finally:
    clean = reap_tree()
if not clean:
    code, text = 125, 'command descendants did not exit within cleanup deadline'
output = tail.decode('utf-8', 'replace') if text is None else text[-limit:]
if truncated:
    output = '[earlier output truncated]\n' + output
try:
    # Completion means descendants have been reaped, not just signalled.
    publish('command.result', {'exit_code': code, 'output': output})
except OSError:
    pass
"""


def _command_record(path, limit):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('container command record must be a regular file')
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('container command record exceeds its byte limit')
    return json.loads(raw)


async def _container_command(environment, child):
    user = f'{os.getuid()}:{os.getgid()}' if os.name == 'posix' else None
    env = {'HOME': '/tmp', **child.env}
    root = Path(child.host_run_dir)
    # Docker owns this detached exec and reaps it. No orphan shell supervisor
    # remains under a container PID 1 that may not reap background processes.
    launch = ['python', '-c', _CONTAINER_SUPERVISOR, child.run_dir,
              str(OUTPUT_BYTES), str(TIMEOUT_S), json.dumps(child.argv)]
    launching = asyncio.create_task(asyncio.to_thread(environment.exec, launch,
        cwd=child.cwd, env=env, user=user, detach=True))
    try:
        code, output = await asyncio.shield(launching)
        if code != 0:
            raise RuntimeError('container command launcher failed: ' + str(output)[-OUTPUT_BYTES:])
        deadline = asyncio.get_running_loop().time() + 10
        while _command_record(root / 'command.ready', 1024) is None:
            result = _command_record(root / 'command.result', OUTPUT_BYTES * 6 + 1024)
            if result is not None:
                return result['exit_code'], result['output']
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError('container command supervisor did not start')
            await asyncio.sleep(.02)
        # The child cannot start before the launch request has released IPC.
        (root / 'command.start').touch(exist_ok=False)
        while True:
            result = _command_record(root / 'command.result', OUTPUT_BYTES * 6 + 1024)
            if result is not None:
                return result['exit_code'], result['output']
            await asyncio.sleep(.02)
    finally:
        with contextlib.suppress(OSError):
            (root / 'command.cancel').touch()
        # A late launcher never receives a start marker; removing its private
        # directory also makes the supervisor exit before spawning any child.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(launching), timeout=5)
        if not launching.done():
            launching.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        with contextlib.suppress(Exception):
            ready = _command_record(root / 'command.ready', 1024)
            if ready is not None:
                # Match Linux process birth time before requesting cleanup:
                # never signal a process that reused the supervisor PID.
                stop = """import os,signal,sys,time
from pathlib import Path
pid, expected = int(sys.argv[1]), sys.argv[2]
path = Path('/proc') / str(pid) / 'stat'
def same():
    try:
        return path.read_text().rsplit(') ',1)[1].split()[19] == expected
    except (FileNotFoundError,ProcessLookupError):
        return False
if same():
    try:
        os.kill(pid,signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline=time.monotonic()+4
    while same() and time.monotonic()<deadline:
        time.sleep(.02)
    if same():
        raise RuntimeError('command supervisor did not finish reaping')
"""
                await asyncio.to_thread(environment.exec,
                    ['python', '-c', stop, str(ready['pid']), ready['start']],
                    cwd=child.cwd, env=env, user=user, timeout=5)


async def _execute(context: CommandContext, *, path: str, settings: dict | None, prompt: str) -> dict:
    from importlib.resources import files
    environment = context.env_obj
    container = context.locus == 'container'
    sdk_path = '/opt/collimate-sdk'
    if not container:
        package = files('collimate')
        if not isinstance(package, Path):
            raise RuntimeError('Host draft commands require a filesystem-backed collimate package')
        if not package.is_dir():
            raise RuntimeError('Host draft commands require an existing collimate package directory')
        sdk_path = str(package.resolve().parent)
    bind_host = await asyncio.to_thread(docker_gateway) if container else '127.0.0.1'
    relay = Forwarder(bind_host=bind_host)
    child = None
    running = None
    try:
        relay.__enter__()
        child = prepare_child(relay, path, settings=settings, prompt=prompt,
            child_workspace=environment.mount if container else gen.workspace_path(),
            ipc_host=bind_host,
            sdk_path=sdk_path)
        running = asyncio.create_task(_container_command(environment, child) if container else _host_command(child))
        try:
            code, output = await asyncio.wait_for(asyncio.shield(running), timeout=TIMEOUT_S)
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            relay.close()
            running.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await running
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {'error': 'test_generator_timeout', 'result': dict(relay.result)}
        # finish is a fire-and-forget SDK frame. Give the listener a bounded
        # chance to drain it after the process has exited before closing it.
        if code == 0:
            await asyncio.to_thread(relay.wait_finished, 0.5)
        return {'exit_code': code, 'output': output.replace(relay.token, '[redacted]'),
                'result': dict(relay.result)}
    finally:
        relay.close()
        if child is not None:
            child.close()
