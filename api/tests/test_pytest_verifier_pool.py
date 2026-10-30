"""Actual subprocess deadlines, fresh engines and worker lifecycle."""
from pathlib import Path
import time

import pytest


@pytest.mark.parametrize('platform,gib', [('linux', 2), ('darwin', 512)])
def test_worker_platform_cap_preserves_other_limits__P4F05(monkeypatch, platform, gib):
    import resource
    import _verifier_worker as worker
    limits = []
    monkeypatch.setattr(worker.sys, 'platform', platform)
    monkeypatch.setattr(worker.tempfile, 'tempdir', None)
    monkeypatch.setattr(worker.resource, 'setrlimit', lambda kind, value: limits.append((kind, value)))
    def eof(_size):
        raise EOFError
    monkeypatch.setattr(worker, 'read_exact', eof)
    worker.main()
    assert limits == [
        (resource.RLIMIT_AS, (gib * 1024**3, gib * 1024**3)),
        (resource.RLIMIT_CPU, (60, 60)),
        (resource.RLIMIT_CORE, (0, 0)),
        (resource.RLIMIT_FSIZE, (0, 0)),
        (resource.RLIMIT_NOFILE, (64, 64)),
    ]


def test_worker_limit_failure_never_reads_a_request__P4F05(monkeypatch):
    import _verifier_worker as worker
    monkeypatch.setattr(worker.tempfile, 'tempdir', None)
    def refuse(_kind, _value):
        raise ValueError('kernel refused address-space cap')
    monkeypatch.setattr(worker.resource, 'setrlimit', refuse)
    monkeypatch.setattr(worker, 'read_exact', lambda _size: pytest.fail('request after failed cap'))
    with pytest.raises(ValueError, match='kernel refused'):
        worker.main()


def test_actual_worker_bundles_and_retains_js_resource_limits__P4F05(tmp_path):
    from verifier_pool import VerifierPool
    with VerifierPool(root=tmp_path / 'workers', cap=1) as pool:
        with pool.lease(time.monotonic() + 10) as lease:
            process = lease.worker.process
            code = ('var __collimate_verifier={default:input=>{'
                    'const b=__collimate_bundle(input.files,input.entrypoint);'
                    'return {valid:typeof b.entrypoint_js==="string" && b.code.includes("__collimate_renderer__")}}}')
            assert lease.verify(code, {'files': {'main.ts': 'export default {__collimate_renderer__:true,component:()=>null}'},
                                       'entrypoint': 'main.ts'}) == {'valid': True}
            for body in ('new ArrayBuffer(70*1024*1024);return {valid:true}',
                         'function recurse(){recurse()};recurse();return {valid:true}'):
                result = lease.verify('var __collimate_verifier={default:()=>{' + body + '}}', {})
                assert result['valid'] is False
                assert result['error'] != 'Verifier timed out'
            assert lease.verify('var __collimate_verifier={default:()=>({valid:true})}', {}) == {'valid': True}
    assert process.poll() is not None
    assert pool.worker_count == 0
    assert not list((tmp_path / 'workers').iterdir())


def test_verifier_pool_fresh_contexts_and_no_ambient_authority__D10(tmp_path):
    from verifier_pool import VerifierPool
    with VerifierPool(root=tmp_path / 'workers', cap=1) as pool:
        with pool.lease(time.monotonic() + 10) as lease:
            assert lease.verify('var __collimate_verifier={default:()=>{globalThis.previous=42;return {valid:true}}}', {}) == {'valid': True}
            assert lease.verify('var __collimate_verifier={default:()=>({valid:typeof previous==="undefined" && typeof process==="undefined" && typeof fetch==="undefined" && typeof require==="undefined"})}', {}) == {'valid': True}
        assert pool.worker_count == 1
    assert pool.worker_count == 0
    assert not list((tmp_path / 'workers').iterdir())


@pytest.mark.parametrize(('body', 'valid'), [('while(true){}', False), ('return {valid:true,toJSON(){while(true){}}}', True), ('return new Proxy({}, {get(){while(true){}}})', False)])
def test_verifier_parent_watchdog_covers_evaluation_and_result_access__D10(tmp_path, body, valid):
    from verifier_pool import VerifierPool
    with VerifierPool(root=tmp_path / 'workers', cap=1, file_seconds=.2) as pool:
        started = time.monotonic()
        with pool.lease(started + 5) as lease:
            result = lease.verify('var __collimate_verifier={default:()=>{' + body + '}}', {})
        # Normalization may safely ignore an unused toJSON field.
        if valid:
            assert result == {'valid': True}
        else:
            assert result['valid'] is False
            assert result['error'] == 'Verifier timed out'
        assert time.monotonic() - started < 3
        with pool.lease(time.monotonic() + 5) as lease:
            assert lease.verify('var __collimate_verifier={default:()=>({valid:true})}', {}) == {'valid': True}


def test_verifier_budget_exhaustion_does_not_start_worker__D10(tmp_path):
    from verifier_pool import VerifierPool, VerifierFailure
    with VerifierPool(root=tmp_path / 'workers') as pool:
        with pytest.raises(VerifierFailure, match='budget exceeded'):
            with pool.lease(time.monotonic() - 1):
                pytest.fail('expired lease admitted')
        assert pool.worker_count == 0


def test_verifier_pool_shutdown_reaps_active_worker_and_descendant__D10(tmp_path):
    """A protocol fixture proves group cleanup independently of native compiler execution."""
    from verifier_pool import VerifierPool
    import threading
    script = tmp_path / 'worker.py'
    marker = tmp_path / 'child.pid'
    script.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
                      'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\n'
                      f'Path({str(marker)!r}).write_text(str(child.pid))\n'
                      'time.sleep(30)\n')
    pool = VerifierPool(root=tmp_path / 'workers', cap=1, _worker_script=script)
    output = []
    def run():
        with pool.lease(time.monotonic() + 5) as lease:
            output.append(lease.verify('unused', {}))
    thread = threading.Thread(target=run)
    thread.start()
    limit = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < limit:
        time.sleep(.01)
    assert marker.exists()
    child = int(marker.read_text())
    pool.close()
    thread.join(2)
    assert not thread.is_alive()
    assert output and output[0]['valid'] is False
    # A dead child may briefly await its OS reaper; it must never be running.
    status = Path(f'/proc/{child}/stat')
    assert not status.exists() or status.read_text().split()[2] == 'Z'
    assert pool.worker_count == 0


@pytest.mark.parametrize('action', [
    'sys.stdout.buffer.write(struct.pack("!I", 70*1024*1024));sys.stdout.buffer.flush()',
    'sys.stdout.buffer.write(struct.pack("!I", 1)+b"x");sys.stdout.buffer.flush()',
    'sys.stderr.buffer.write(b"x"*1000000);sys.stderr.buffer.flush();time.sleep(5)',
    'sys.exit(1)',
])
def test_verifier_protocol_faults_are_bounded_and_reaped__D10(tmp_path, action):
    from verifier_pool import VerifierPool
    script = tmp_path / 'fault.py'
    script.write_text('import sys,struct,time\n' + action + '\n')
    with VerifierPool(root=tmp_path / 'workers', cap=1, file_seconds=.2, _worker_script=script) as pool:
        with pool.lease(time.monotonic() + 5) as lease:
            assert lease.verify('unused', {})['valid'] is False
        assert pool.worker_count == 0
        assert not list((tmp_path / 'workers').iterdir())


def test_worker_reuse_does_not_apply_five_second_lifetime_cpu_limit__D10(tmp_path):
    from verifier_pool import VerifierPool
    with VerifierPool(root=tmp_path / 'workers', cap=1) as pool:
        started = time.monotonic()
        with pool.lease(started + 15) as lease:
            pid = lease.worker.process.pid
            for _ in range(7):
                result = lease.verify('var __collimate_verifier={default:()=>{const until=Date.now()+800;while(Date.now()<until){}return {valid:true}}}', {})
                assert result == {'valid': True}
                assert lease.worker.process.pid == pid
        assert time.monotonic() - started >= 5


def test_cancelled_pool_admission_never_adds_a_worker__D10(tmp_path):
    from verifier_pool import VerifierPool, VerifierFailure
    import threading
    cancelled = threading.Event()
    cancelled.set()
    with VerifierPool(root=tmp_path / 'workers', cap=1) as pool:
        with pytest.raises(VerifierFailure, match='cancelled'):
            with pool.lease(time.monotonic()+5, cancelled=cancelled):
                pytest.fail('cancelled lease admitted')
        assert pool.worker_count == 0
