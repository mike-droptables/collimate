"""P4F09: exercise the seam with real canonical compilation/evaluation."""
import base64
import hashlib
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest


def adapter_module():
    path = Path(__file__).resolve().parents[2] / 'shim/collimate_playground/verification.py'
    spec = importlib.util.spec_from_file_location('supervised_adapter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def adapter():
    return adapter_module()


@pytest.fixture
def actual_bridge(adapter):
    import collimate._host.renderer_bundle as compiler
    from collimate._host.verification import VERIFICATION_BOUNDARY, run_verifier
    native = compiler._native
    calls = []

    def invoke(op, encoded, remaining_ms, cancelled):
        assert remaining_ms > 0
        payload = json.loads(encoded)
        calls.append((op, payload))
        if op == 'acquire':
            return json.dumps({'lease_id': 'owned-test-worker'})
        if op in ('release', 'close'):
            return '{}'
        deadline = time.monotonic() + remaining_ms / 1000
        if op == 'compile':
            files = {path: base64.b64decode(body, validate=True) for path, body in payload['files']}
            code, transformed = native(files, payload['entrypoint'], payload['policy'], deadline,
                                       True, verifier=payload['verifier'])
            return json.dumps({'code': code, 'entrypoint_js': transformed})
        assert op == 'evaluate' and payload['boundary'] == VERIFICATION_BOUNDARY
        return json.dumps(run_verifier(payload['code'], json.loads(payload['input_json']), deadline=deadline))

    invoke.calls = calls
    return invoke


def test_P4F09_canonical_compile_identity_and_actual_negative_verdict__P6F01(adapter, actual_bridge, monkeypatch):
    import collimate._host.renderer_bundle as compiler
    from collimate.shared import shared_path
    source = b'export default (input) => input.text === "good" ? {valid:true} : {valid:false,error:"needs good"};'
    prelude = shared_path('verify', 'prelude.js').read_bytes()
    before = compiler.compile_verifier(source, prelude=prelude)
    monkeypatch.setattr(compiler, '_native', adapter.native_compiler(actual_bridge))
    after = compiler.compile_verifier(source, prelude=prelude)
    assert after == before
    assert after.source_hash == compiler.verifier_source_hash(source, prelude=prelude)
    assert after.bundle_hash == hashlib.sha256(after.code.encode()).hexdigest()
    pool = adapter.VerifierPool(invoke=actual_bridge)
    with pool.lease() as lease:
        assert lease.verify(after.code, {'text': 'good'}) == {'valid': True}
        assert lease.verify(after.code, {'text': 'bad'}) == {'valid': False, 'error': 'needs good'}
    pool.close()
    assert [op for op, _ in actual_bridge.calls] == ['compile', 'acquire', 'evaluate', 'evaluate', 'release', 'close']


@pytest.mark.parametrize('path', ['../escape.ts', '_runs/x.ts', 'C:main.tsx'])
def test_P4F09_canonical_compiler_rejects_paths_before_bridge__P6F01(adapter, path):
    invoke = lambda *args: pytest.fail('invalid canonical input crossed bridge')
    from collimate._host.renderer_bundle import CONTRACT, RendererCompileError
    with pytest.raises(RendererCompileError):
        adapter.native_compiler(invoke)({path: b''}, path, CONTRACT, time.monotonic() + 5, True)


@pytest.mark.parametrize('failure', ['expired', 'cancelled', 'late', 'protocol', 'oversized', 'invalid'])
def test_P4F09_leases_expiry_cancellation_frames_and_refusal(adapter, monkeypatch, failure):
    calls = []
    cancel = threading.Event()
    clock = [100.0]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(adapter, 'MAX_FRAME', 2000)

    def invoke(op, encoded, remaining_ms, cancelled):
        calls.append((op, json.loads(encoded)))
        if op == 'acquire':
            return '{"lease_id":"owned"}'
        if op == 'evaluate':
            assert cancelled is not None and not cancelled()
            if failure == 'late':
                clock[0] = 106
            if failure == 'protocol':
                return 'not JSON'
            if failure == 'invalid':
                return '{"valid":1}'
            return '{"valid":true}'
        return '{}'

    pool = adapter.VerifierPool(invoke=invoke)
    with pool.lease(cancelled=cancel) as lease:
        if failure == 'expired':
            clock[0] = 131
        if failure == 'cancelled':
            cancel.set()
        code = 'x' * 3000 if failure == 'oversized' else 'actual compiled code'
        result = lease.verify(code, {})
        assert result['valid'] is False and result['error']
    evaluate_count = sum(op == 'evaluate' for op, _ in calls)
    assert evaluate_count == (failure in ('late', 'protocol', 'invalid'))
    assert calls[-1] == ('release', {'lease_id': 'owned', 'discard': True})


def test_P4F09_expiry_during_acquisition_releases_owned_slot(adapter, monkeypatch):
    import verifier_pool
    clock = [100.0]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    calls = []

    def invoke(op, encoded, remaining_ms, cancelled):
        calls.append(op)
        if op == 'acquire':
            clock[0] = 106
            return '{"lease_id":"late-owned"}'
        return '{}'

    with (pytest.raises(verifier_pool.VerifierFailure, match='Verifier busy, try again'),
          adapter.VerifierPool(invoke=invoke).lease(deadline=105)):
        pytest.fail('expired lease yielded')
    assert calls == ['acquire', 'release']


def test_P4F09_actual_host_cache_and_admission_use_accepted_source(adapter, actual_bridge, monkeypatch):
    import collimate._host.renderer_bundle as compiler
    import package_approval
    import renderer_bundles
    import verification as host
    source = b'export default input => ({valid:false,error:"accepted verifier refused package"});'
    monkeypatch.setattr(compiler, '_native', adapter.native_compiler(actual_bridge))
    storage = {}
    monkeypatch.setattr(renderer_bundles, 'get_bundle', storage.get)
    monkeypatch.setattr(renderer_bundles, 'publish_server_bundle', lambda identity, body: storage.update({identity: body}))
    monkeypatch.setattr(host, 'get_verifier_pool', lambda: adapter.VerifierPool(invoke=actual_bridge))
    # Keep the real OrderedDict cache API, including move_to_end and copy returns.
    from collections import OrderedDict
    monkeypatch.setattr(host, '_verdicts', OrderedDict())
    files = {'synthetic.generator': b'id: synthetic\nname: Synthetic\nentrypoint: main.py\n', 'main.py': b'print(1)'}
    import builtin_packages
    monkeypatch.setattr(builtin_packages, 'verifier_for', lambda path: source)
    with pytest.raises(ValueError, match='accepted verifier refused package'):
        package_approval.admit_package(None, 'pkg/synthetic.generator', files)
    evaluation = next(payload for op, payload in actual_bridge.calls if op == 'evaluate')
    value = json.loads(evaluation['input_json'])
    assert value == host.verifier_input('pkg/synthetic.generator', {'pkg/' + p: b for p, b in files.items()}, ['pkg/' + p for p in files])
    assert host.verify_source(source, value) == {'valid': False, 'error': 'accepted verifier refused package'}
    assert sum(op == 'evaluate' for op, _ in actual_bridge.calls) == 1
    assert host.verify_source(source, value, deadline=time.monotonic() - 1) == {'valid': False, 'error': 'verifier budget exceeded'}


def test_P4F09_discarded_worker_reacquired_within_same_lease(adapter):
    calls = []
    acquisitions = [0]

    def invoke(op, encoded, remaining_ms, cancelled):
        payload = json.loads(encoded)
        calls.append((op, payload))
        if op == 'acquire':
            acquisitions[0] += 1
            return json.dumps({'lease_id': str(acquisitions[0])})
        if op == 'evaluate':
            if payload['lease_id'] == '1':
                raise RuntimeError('Verifier timed out')
            return '{"valid":true}'
        return '{}'

    pool = adapter.VerifierPool(invoke=invoke)
    with pool.lease() as lease:
        assert lease.verify('code', {}) == {'valid': False, 'error': 'Verifier timed out'}
        assert lease.token is None
        assert lease.verify('code', {}) == {'valid': True}
    assert [op for op, _ in calls] == ['acquire', 'evaluate', 'release', 'acquire', 'evaluate', 'release']
    assert calls[2][1] == {'lease_id': '1', 'discard': True}
    assert calls[-1][1] == {'lease_id': '2', 'discard': False}


def test_P4F09_renderer_compile_transform_parity_without_source_execution(adapter, actual_bridge, monkeypatch):
    import collimate._host.renderer_bundle as compiler
    files = {'view:main.tsx': b'throw new Error("DO NOT EXECUTE"); export default {__collimate_renderer__:true,component:()=>null};'}
    before = compiler.bundle_renderer(files, entrypoint='view:main.tsx')
    monkeypatch.setattr(compiler, '_native', adapter.native_compiler(actual_bridge))
    after = compiler.bundle_renderer(files, entrypoint='view:main.tsx')
    assert after == before
    assert 'DO NOT EXECUTE' in after.entrypoint_js


@pytest.mark.parametrize('body,valid', [
    ('return {valid:true,toJSON(){while(true){}}}', True),
    ('let n=0;return {get valid(){return n++===0 ? true : "bad"}}', True),
    ('JSON.stringify=()=>"bad";Array.isArray=()=>true;return {valid:true}', True),
    ('return {valid:false,error:"x".repeat(10000)}', False),
    ('return Promise.resolve({valid:true})', False),
    ('return {valid:true,then(){}}', False),
    ('return []', False),
    ('return {valid:false,error:42}', False),
])
def test_P4F09_exact_canonical_boundary_normalizes_actual_engine(adapter, actual_bridge, body, valid):
    code = 'globalThis.__collimate_verifier={default(input){' + body + '}}'
    with adapter.VerifierPool(invoke=actual_bridge).lease() as lease:
        result = lease.verify(code, {})
    assert result['valid'] is valid
    if valid:
        assert result == {'valid': True}
    else:
        assert isinstance(result['error'], str) and 0 < len(result['error']) <= 4096


def test_P4F09_transient_resource_failure_not_cached_by_unchanged_host(adapter, actual_bridge, monkeypatch):
    from collections import OrderedDict

    import collimate._host.renderer_bundle as compiler
    import renderer_bundles
    import verification as host
    monkeypatch.setattr(compiler, '_native', adapter.native_compiler(actual_bridge))
    monkeypatch.setattr(renderer_bundles, 'get_bundle', lambda identity: None)
    monkeypatch.setattr(renderer_bundles, 'publish_server_bundle', lambda identity, body: None)
    monkeypatch.setattr(host, '_verdicts', OrderedDict())
    attempts = [0]

    def invoke(op, encoded, remaining_ms, cancelled):
        if op == 'evaluate':
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError('Verifier engine exhausted its resource limit')
        return actual_bridge(op, encoded, remaining_ms, cancelled)

    monkeypatch.setattr(host, 'get_verifier_pool', lambda: adapter.VerifierPool(invoke=invoke))
    source = b'export default input => ({valid:true});'
    assert host.verify_source(source, {}) == {'valid': False, 'error': 'Verifier engine exhausted its resource limit'}
    assert not host._verdicts
    assert host.verify_source(source, {}) == {'valid': True}
    assert attempts[0] == 2 and len(host._verdicts) == 1


@pytest.mark.parametrize('prefix', ['', 'Error: '])
def test_P4R02_busy_refusal_preserves_bytes_and_is_not_cached(adapter, actual_bridge, monkeypatch, prefix):
    from collections import OrderedDict
    import collimate._host.renderer_bundle as compiler
    import renderer_bundles
    import verification as host
    monkeypatch.setattr(compiler, '_native', adapter.native_compiler(actual_bridge))
    monkeypatch.setattr(renderer_bundles, 'get_bundle', lambda identity: None)
    monkeypatch.setattr(renderer_bundles, 'publish_server_bundle', lambda identity, body: None)
    monkeypatch.setattr(host, '_verdicts', OrderedDict())
    attempts = []
    budgets = []
    def invoke(op, encoded, remaining_ms, cancelled):
        if op == 'evaluate':
            attempts.append(json.loads(encoded))
            budgets.append(remaining_ms)
            if len(attempts) == 1:
                raise RuntimeError(prefix + adapter.BUSY)
        return actual_bridge(op, encoded, remaining_ms, cancelled)
    monkeypatch.setattr(host, 'get_verifier_pool', lambda: adapter.VerifierPool(invoke=invoke))
    source = b'export default input => ({valid:input.text === "good",error:"needs good"});'
    files = {'sample.check': b'good'}
    original = files.copy()
    value = host.verifier_input('sample.check', files, ['sample.check'])
    assert host.verify_source(source, value) == {'valid': False, 'error': adapter.BUSY}
    assert not host._verdicts and files == original
    assert host.verify_source(source, value) == {'valid': True}
    assert len(attempts) == 2 and len(host._verdicts) == 1
    assert all(0 < budget <= 5000 for budget in budgets)
    assert json.loads(attempts[0]['input_json']) == json.loads(attempts[1]['input_json']) == value
    assert host.verify_source(source, {**value, 'text': 'bad'}) == {'valid': False, 'error': 'needs good'}
    assert len(host._verdicts) == 2 and files == original


def test_P4R02_expiry_message_retains_transient_host_prefix(adapter, monkeypatch):
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: 10)
    with pytest.raises(adapter.BridgeFailure, match='^Verifier busy, try again'):
        adapter._call(lambda *args: pytest.fail('expired operation crossed bridge'), 'acquire', {}, 9)
    assert adapter.FILE_SECONDS == 5 and adapter.FLUSH_SECONDS == 30


@pytest.mark.parametrize('message', ['Error: arbitrary worker failure', 'Error: Verifier busy, try again (deadline exceeded) extra'])
def test_P4R02_unrecognized_bridge_errors_keep_protocol_failure(adapter, message):
    def invoke(*args):
        raise RuntimeError(message)
    with pytest.raises(adapter.BridgeFailure) as refusal:
        adapter._call(invoke, 'acquire', {}, time.monotonic() + 5)
    assert str(refusal.value) == 'Verifier worker protocol failed: ' + message


def refusal_records(capsys):
    prefix = '[collimate-verifier-refusal] '
    return [json.loads(line[len(prefix):]) for line in capsys.readouterr().out.splitlines()
            if line.startswith(prefix)]


def test_P4R02_outer_host_expiry_logged_and_presented_busy(adapter, monkeypatch, capsys, confined_host):
    import verification as host
    monkeypatch.setattr(host, 'compiled_verifier', lambda *args, **kwargs: pytest.fail('expired host dispatched'))
    result = adapter._host_verifier(host.verify_source)(b'secret source', {'text': 'secret input'}, deadline=time.monotonic() - 1)
    assert result == {'valid': False, 'error': adapter.BUSY}
    records = refusal_records(capsys)
    assert len(records) == 1 and records[0]['op'] == 'verify'
    assert records[0]['reason'] == 'deadline exceeded; verifier busy, try again'
    assert records[0]['elapsed_ms'] >= 0


@pytest.mark.parametrize('case,reason', [
    ('expired', 'deadline exceeded; verifier busy, try again'),
    ('cancelled', 'verification cancelled'),
    ('json', 'invalid verifier input'),
    ('frame', 'invalid verifier input'),
    ('late', 'deadline exceeded; verifier busy, try again'),
    ('worker', 'verifier protocol refused'),
])
def test_P4R02_local_bridge_refusals_have_fixed_safe_records(adapter, monkeypatch, capsys, case, reason):
    clock = [100.]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    cancelled = threading.Event()
    if case == 'cancelled':
        cancelled.set()
    if case == 'frame':
        monkeypatch.setattr(adapter, 'MAX_FRAME', 10)
    def invoke(*args):
        if case == 'late':
            clock[0] = 106
        if case == 'worker':
            raise RuntimeError('SECRET_SENTINEL_83')
        return '{}'
    payload = {'text': object()} if case == 'json' else {'text': 'SECRET_SENTINEL_83'}
    with pytest.raises(adapter.BridgeFailure):
        adapter._call(invoke, 'SECRET_SENTINEL_83', payload,
                      99 if case == 'expired' else 105, cancelled)
    records = refusal_records(capsys)
    assert len(records) == 1
    assert records[0]['op'] == 'bridge' and records[0]['reason'] == reason
    assert records[0]['elapsed_ms'] >= 0
    assert 'SECRET_SENTINEL_83' not in json.dumps(records)


def test_P4R02_installed_actual_admission_and_cached_negative_emit_once(adapter, actual_bridge, monkeypatch, capsys):
    from collections import OrderedDict
    import collimate._host.renderer_bundle as compiler
    import verifier_pool
    import verification as host
    import package_approval
    import renderer_bundles
    # Retain real module restoration while exercising production installation.
    for module, names in [(compiler, ['_native']),
                          (verifier_pool, ['VerifierPool', 'get_verifier_pool', 'close_verifier_pool']),
                          (host, ['verify_source', 'validate_file', 'get_verifier_pool'])]:
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(host, '_verdicts', OrderedDict())
    monkeypatch.setattr(renderer_bundles, 'get_bundle', lambda identity: None)
    monkeypatch.setattr(renderer_bundles, 'publish_server_bundle', lambda identity, body: None)
    adapter.install(actual_bridge)
    source = b'export default input => ({valid:false,error:input.text});'
    body = b'id: synthetic\nname: SECRET_SENTINEL_83\nentrypoint: main.py\n'
    files = {'synthetic.generator': body, 'main.py': b'print(1)'}
    before = files.copy()
    import builtin_packages
    monkeypatch.setattr(builtin_packages, 'verifier_for', lambda path: source)
    for _ in range(2):
        with pytest.raises(ValueError, match='SECRET_SENTINEL_83'):
            package_approval.admit_package(None, 'pkg/synthetic.generator', files)
        records = refusal_records(capsys)
        assert len(records) == 1 and records[0]['op'] == 'verify'
        assert records[0]['reason'] == 'content refused'
        assert 'SECRET_SENTINEL_83' not in json.dumps(records)
    assert sum(op == 'evaluate' for op, _ in actual_bridge.calls) == 1
    assert files == before and len(host._verdicts) == 1
    verifier_pool.close_verifier_pool()


def test_P4R02_nested_postbridge_expiry_logs_only_outer_attempt(adapter, monkeypatch, capsys):
    import verification as host
    clock = [100.]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(host, 'compiled_verifier', lambda *args, **kwargs: ('hash', 'code'))
    from collections import OrderedDict
    monkeypatch.setattr(host, '_verdicts', OrderedDict())
    def invoke(op, *args):
        if op == 'acquire':
            return '{"lease_id":"owned"}'
        if op == 'evaluate':
            clock[0] = 106
            return '{"valid":true}'
        return '{}'
    monkeypatch.setattr(host, 'get_verifier_pool', lambda: adapter.VerifierPool(invoke=invoke))
    assert adapter._host_verifier(host.verify_source)(b'source', {}) == {'valid': False, 'error': adapter.BUSY}
    assert not host._verdicts
    records = refusal_records(capsys)
    assert len(records) == 1 and records[0]['op'] == 'verify'
    assert records[0]['reason'] == 'deadline exceeded; verifier busy, try again'


def test_P9_run_flush_expiry_is_busy_and_logged_once(adapter, monkeypatch, capsys):
    """The run flush builds its expired verdict with the host's one
    constructor, so the build's patch presents it as the busy refusal too."""
    from types import SimpleNamespace
    from conftest import refusal_overlay_module
    run = refusal_overlay_module('run_verification', adapter, monkeypatch)
    verifier = run.RunVerifier(SimpleNamespace(channel='demo'))
    verifier.deadline = 0
    monkeypatch.setattr(verifier, '_targets', lambda snap, files: {'a.checked': b'source'})
    snap = SimpleNamespace(policy_refused={}, writes={'a.checked': 'digest'}, candidate=lambda refused=(): {})
    assert verifier.check(snap) == {'a.checked': run.refusal('a.checked', adapter.BUSY)}
    records = refusal_records(capsys)
    assert len(records) == 1 and records[0]['op'] == 'verify'
    assert records[0]['reason'] == 'deadline exceeded; verifier busy, try again'


def test_P4R02_local_lease_input_refusal_observed_once(adapter, capsys):
    def invoke(op, *args):
        return '{"lease_id":"owned"}' if op == 'acquire' else '{}'
    with adapter.VerifierPool(invoke=invoke).lease() as lease:
        assert lease.verify('code', 'SECRET_SENTINEL_83') == {'valid': False, 'error': 'invalid verifier input'}
    records = refusal_records(capsys)
    assert len(records) == 1 and records[0]['op'] == 'evaluate'
    assert records[0]['reason'] == 'invalid verifier input'


@pytest.fixture
def confined_host(adapter, monkeypatch):
    from conftest import refusal_overlay_module
    return refusal_overlay_module('verification', adapter, monkeypatch)


@pytest.mark.parametrize('authored', ['verifier budget exceeded', 'Verifier busy, try again (deadline exceeded)'])
def test_P4R02_authored_deadline_text_is_content_byte_exact(adapter, actual_bridge, monkeypatch, capsys, confined_host, authored):
    from collections import OrderedDict
    import collimate._host.renderer_bundle as compiler
    import renderer_bundles
    import verification as host
    monkeypatch.setattr(compiler, '_native', adapter.native_compiler(actual_bridge))
    monkeypatch.setattr(renderer_bundles, 'get_bundle', lambda identity: None)
    monkeypatch.setattr(renderer_bundles, 'publish_server_bundle', lambda identity, body: None)
    monkeypatch.setattr(host, '_verdicts', OrderedDict())
    monkeypatch.setattr(host, 'get_verifier_pool', lambda: adapter.VerifierPool(invoke=actual_bridge))
    source = ('export default ()=>({valid:false,error:' + json.dumps(authored) + '});').encode()
    expected = {'valid': False, 'error': authored}
    assert adapter._host_verifier(host.verify_source)(source, {}, deadline=time.monotonic()+5) == expected
    records = refusal_records(capsys)
    assert len(records) == 1 and records[0]['reason'] == 'content refused'
    assert records[0]['op'] == 'verify'
