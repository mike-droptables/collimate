"""Protocol fault injection is separate from real compiler behavior tests."""
import os
import json
from pathlib import Path
import resource
import signal
import struct
import subprocess
import sys
import time

import pytest

from collimate import _esbuild_service as service
from collimate import _compiler_child as compiler_child

# A compiler version no real esbuild has: these tests fake the service.
FAKE = '0.0.0-test'


@pytest.mark.parametrize('platform,gib', [('linux', 2), ('darwin', 512)])
def test_compiler_platform_caps_preserve_remaining_limits__P4F05(monkeypatch, platform, gib):
    limits = []
    executed = []
    monkeypatch.setattr(compiler_child.sys, 'platform', platform)
    monkeypatch.setattr(compiler_child.sys, 'argv', ['launcher', '/verified/compiler', FAKE])
    monkeypatch.setattr(compiler_child.resource, 'setrlimit', lambda kind, value: limits.append((kind, value)))
    monkeypatch.setattr(compiler_child.os, 'execve', lambda *args: executed.append(args))
    compiler_child.main()
    assert limits == [
        (resource.RLIMIT_CPU, (4, 4)),
        (resource.RLIMIT_AS, (gib * 1024**3, gib * 1024**3)),
        (resource.RLIMIT_CORE, (0, 0)),
        (resource.RLIMIT_NOFILE, (64, 64)),
    ]
    assert executed == [('/verified/compiler', ['/verified/compiler', '--service=' + FAKE],
                         {'PATH': '', 'LANG': 'C.UTF-8', 'GOMEMLIMIT': '128MiB', 'GOMAXPROCS': '2'})]


def test_compiler_limit_failure_never_executes_unbounded__P4F05(monkeypatch):
    monkeypatch.setattr(compiler_child.sys, 'argv', ['launcher', '/verified/compiler', FAKE])
    def refuse(kind, value):
        if kind == resource.RLIMIT_AS:
            raise ValueError('kernel refused address-space cap')
    monkeypatch.setattr(compiler_child.resource, 'setrlimit', refuse)
    monkeypatch.setattr(compiler_child.os, 'execve', lambda *args: pytest.fail('exec after failed limit'))
    with pytest.raises(ValueError, match='kernel refused'):
        compiler_child.main()


def test_compiler_real_limits_survive_exec_and_refuse_over_cap_mapping__P4F05(tmp_path):
    # Exercise real kernel limits without committing a large physical allocation.
    probe = r'''
import errno,json,mmap,os,resource
limits={name:resource.getrlimit(getattr(resource,name)) for name in ('RLIMIT_CPU','RLIMIT_AS','RLIMIT_CORE','RLIMIT_NOFILE')}
with mmap.mmap(-1,4096) as small:
    small[0]=42
try:
    mapping=mmap.mmap(-1,limits['RLIMIT_AS'][0]+4096,flags=mmap.MAP_PRIVATE|mmap.MAP_ANON,prot=0)
except OSError as error:
    assert error.errno==errno.ENOMEM, error
else:
    mapping.close()
    raise AssertionError('kernel allowed an over-cap reservation')
print(json.dumps({'limits':limits,'go_target':os.environ['GOMEMLIMIT'],'over_cap':'ENOMEM'}))
'''
    wrapper = rf'''
import os,runpy,sys
launcher,probe=sys.argv[1:]
execute=os.execve
def exec_probe(executable,argv,env):
    execute(sys.executable,[sys.executable,'-I','-c',probe],env)
os.execve=exec_probe
sys.argv=[launcher,'/test/exec-target',{FAKE!r}]
runpy.run_path(launcher,run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-I', '-c', wrapper,
                             str(Path(compiler_child.__file__).resolve()), probe],
                            cwd=tmp_path, env={'PATH': '', 'LANG': 'C.UTF-8'},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    cap = (512 if sys.platform == 'darwin' else 2) * 1024**3
    assert observed == {'limits': {'RLIMIT_CPU': [4, 4], 'RLIMIT_AS': [cap, cap],
                                   'RLIMIT_CORE': [0, 0], 'RLIMIT_NOFILE': [64, 64]},
                        'go_target': '128MiB', 'over_cap': 'ENOMEM'}


def packet(value, identifier=3, response=False):
    return struct.pack('<I', (identifier << 1) | response) + service.encode(value)


def test_service_codec_roundtrip_and_bounds__F1984():
    value = {'text': 'π𐀀', 'raw': b'\0\xff', 'list': [True, False, None, -3]}
    assert service.decode(packet(value)) == (3, False, value)
    for bad in (b'', packet(value)[:-1], packet(None)+b'junk', struct.pack('<I', 0)+b'\x09', struct.pack('<I', 0)+b'\1\2', struct.pack('<I', 0)+b'\5'+struct.pack('<I', 2**32-1)):
        with pytest.raises((service.ServiceError, UnicodeError)):
            service.decode(bad)
    nested = None
    for _ in range(70):
        nested = [nested]
    with pytest.raises(service.ServiceError, match='nesting'):
        service.encode(nested)


@pytest.fixture
def fake_service(monkeypatch, tmp_path):
    real_popen = service.subprocess.Popen
    processes = []
    scripts = []
    def launch(body):
        path = tmp_path / f'fake-{len(scripts)}.py'
        path.write_text('import os,sys,time,struct\n' + body)
        scripts.append(path)
        def replace(argv, **kwargs):
            process = real_popen([sys.executable, '-I', str(path)], **kwargs)
            processes.append(process)
            return process
        monkeypatch.setattr(service.subprocess, 'Popen', replace)
        return processes
    yield launch
    assert all(p.poll() is not None for p in processes), 'native child was not reaped'


def test_service_reads_split_handshake_and_coalesced_frames__F1984(fake_service):
    answer = packet({'errors': [], 'value': 42}, 0, True)
    fake_service("wire=struct.pack('<I'," + str(len(FAKE)) + ")+" + repr(FAKE.encode()) + "\nfor b in wire: os.write(1,bytes([b]));time.sleep(.001)\nos.write(1," + repr(struct.pack('<I', len(answer)) + answer) + ")\ntime.sleep(1)\n")
    client = service.Service('/unused', FAKE, time.monotonic()+1, True)
    try:
        assert client.request({'command': 'probe'})['value'] == 42
    finally:
        client.close()


@pytest.mark.parametrize('body,match', [
    ("os.write(1,struct.pack('<I',3)+b'bad')\ntime.sleep(10)\n", 'version'),
    ("os.write(1,struct.pack('<I',2**32-1))\ntime.sleep(10)\n", 'frame'),
    ("time.sleep(10)\n", 'deadline'),
    ("sys.exit(2)\n", 'stopped'),
    ("while True: os.write(2,b'x'*65536)\n", 'deadline|output'),
])
def test_service_startup_fault_kills_and_reaps__F1984(fake_service, body, match):
    fake_service(body)
    with pytest.raises(service.ServiceError, match=match):
        service.Service('/unused', FAKE, time.monotonic()+.15, True)


def test_service_rejects_response_identity_and_cleans_child__F1984(fake_service):
    answer = packet({'errors': []}, 91, True)
    fake_service("os.write(1,struct.pack('<I'," + str(len(FAKE)) + ")+" + repr(FAKE.encode()) + ")\nos.write(1," + repr(struct.pack('<I', len(answer)) + answer) + ")\ntime.sleep(10)\n")
    client = service.Service('/unused', FAKE, time.monotonic()+1, True)
    try:
        with pytest.raises(service.ServiceError, match='identifier'):
            client.request({'command': 'probe'})
    finally:
        client.close()


def test_service_cancellation_kills_child_without_killing_worker_group__F1984(fake_service):
    processes = fake_service("os.write(1,struct.pack('<I'," + str(len(FAKE)) + ")+" + repr(FAKE.encode()) + ")\ntime.sleep(10)\n")
    client = service.Service('/unused', FAKE, time.monotonic()+1, False)
    assert os.getpgid(processes[0].pid) == os.getpgrp()
    client.close()
    assert processes[0].returncode == -signal.SIGKILL
