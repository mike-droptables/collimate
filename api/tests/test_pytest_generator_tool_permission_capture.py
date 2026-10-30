"""Private permission RPC capture; simulated transport is never native proof."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests_live import test_live_generator_tool as live

SDK = Path(__file__).resolve().parents[2] / 'sdk'
GENERATORS = Path(__file__).resolve().parents[2] / 'content' / 'generators'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker():
    return load(live.FIXTURES / 'generator_tool_observed_agent.py', 'permission_test_worker')


def observer():
    return load(SDK / 'tests_live/fixtures/acp_capture.py', 'permission_test_capture')


def rpc(number):
    return {'jsonrpc': '2.0', 'id': number, 'method': 'session/request_permission',
        'params': {'sessionId': 'native', 'toolCall': {'toolCallId': f'call-{number}',
            'rawInput': {'path': 'draft/probe.generator', 'settings': {},
                         'prompt': 'synthetic-selected-secret-' * 100}},
            'options': [{'optionId': 'allow', 'kind': 'allow_once', 'name': 'Allow'}]}}


@pytest.mark.parametrize('ending', ['normal', 'error', 'cancel'])
def test_permission_rpc_envelope_is_private_disjoint_and_unwinds_D23(ending):
    async def scenario():
        module, capture, logs, inputs = worker(), observer(), [], []
        router = module.TurnObserver(capture, backend='qwen-code', provenance={'sources': {}},
            identity=lambda: {'run_id': 'run', 'cell_id': 'cell'},
            scrub=lambda value: json.loads(json.dumps(value).replace('synthetic-selected-secret', '[redacted]')),
            write_log=lambda path, body, **kwargs: logs.append((path, json.loads(body), kwargs)))
        async def original(**kwargs):
            number = router.ordinal
            request = rpc(number)
            reply = {'jsonrpc': '2.0', 'id': number, 'result': {
                'outcome': {'outcome': 'selected', 'optionId': 'allow'}}}
            inputs.append(deepcopy((request, reply)))
            router.observe('send', {'id': number, 'method': 'session/prompt',
                'params': {'sessionId': 'native', 'prompt': 'unretained prompt'}})
            router.observe('receive', request)
            router.observe('send', reply)
            router.observe('receive', {'id': number, 'result': {'stopReason': 'end_turn'}})
            assert logs == [] if number == 1 else len(logs) == 1
            assert (request, reply) == tuple(inputs[-1])
            if number == 1 and ending == 'error':
                raise ValueError('synthetic original failure')
            if number == 1 and ending == 'cancel':
                raise asyncio.CancelledError()
            return 'original result'
        wrapped = router.wrap(original)
        if ending == 'normal':
            assert await wrapped(backend='qwen-code') == 'original result'
        else:
            with pytest.raises(ValueError if ending == 'error' else asyncio.CancelledError):
                await wrapped(backend='qwen-code')
        assert router.current is None
        assert await wrapped(backend='qwen-code') == 'original result'
        assert router.current is None and len(logs) == 2
        assert [item[1]['outcome'] for item in logs] == [ending, 'normal']
        for number, (path, body, kwargs) in enumerate(logs, 1):
            assert path == f'_agent/d23-native/turn-{number:04d}.json' and kwargs == {'category': 'log'}
            assert body['ordinal'] == number and body['run_id'] == 'run' and body['cell_id'] == 'cell'
            assert [item['direction'] for item in body['permission_records']] == ['receive', 'send']
            assert [item['after_record_count'] for item in body['permission_records']] == [1, 1]
            assert [item['message']['id'] for item in body['permission_records']] == [number, number]
            assert len(body['records']) == 2 and body['records'][0] == {
                'direction': 'send', 'id': number, 'method': 'session/prompt', 'sessionId': 'native'}
        assert 'synthetic-selected-secret' not in json.dumps(logs)
        assert 'unretained prompt' not in json.dumps(logs)
    asyncio.run(scenario())


def test_permission_bound_failure_stages_partial_evidence_and_preserves_original_error_D23():
    async def scenario():
        module, capture, logs = worker(), observer(), []
        class Tiny(capture.Capture):
            def __init__(self):
                super().__init__(max_records=1)
        router = module.TurnObserver(SimpleNamespace(Capture=Tiny), backend='qwen-code',
            provenance={}, identity=lambda: {}, scrub=deepcopy,
            write_log=lambda path, body, **kwargs: logs.append(json.loads(body)))
        async def original(**kwargs):
            router.observe('receive', rpc(1))
            router.observe('send', {'jsonrpc': '2.0', 'id': 1, 'result': {}})
        with pytest.raises(capture.CaptureError, match='bound'):
            await router.wrap(original)(backend='qwen-code')
        assert router.current is None and logs[0]['outcome'] == 'error'
        assert len(logs[0]['permission_records']) == 1 and logs[0]['records'] == []
    asyncio.run(scenario())


def test_permission_real_private_writer_scrubs_and_stages_only_as_log_D23(tmp_path, monkeypatch):
    import importlib
    import stat
    import gen
    from collimate.testing import run_generator_callable
    monkeypatch.syspath_prepend(str(GENERATORS))
    hygiene = importlib.import_module('_agent_shared.secret_hygiene')
    module = worker()
    async def entry():
        hygiene.remember('synthetic-selected-secret')
        router = module.TurnObserver(observer(), backend='qwen-code', provenance={},
            identity=lambda: {'run_id': gen.run_info().run_id},
            scrub=hygiene.scrub_object, write_log=module.private_log)
        async def original(**kwargs):
            request = rpc(1)
            request['params']['toolCall']['rawInput']['synthetic-selected-secret'] = 'synthetic-selected-secret'
            router.observe('receive', request)
            router.observe('send', {'jsonrpc': '2.0', 'id': 1,
                'result': {'outcome': {'outcome': 'selected', 'optionId': 'allow'},
                           'diagnostic': 'synthetic-selected-secret'}})
        await router.wrap(original)(backend='qwen-code')
        await gen.commit()
    try:
        result = run_generator_callable(entry, workspace=tmp_path, run_id='permission-observer')
    finally:
        hygiene.clear()
    assert result.ok, result.error
    relative = '_agent/d23-native/turn-0001.json'
    assert relative in result.log_files and relative not in result.files
    path = tmp_path / relative
    body = path.read_text()
    assert 'synthetic-selected-secret' not in body and '[redacted]' in body
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    envelope = json.loads(body)
    assert envelope['records'] == [] and len(envelope['permission_records']) == 2
    assert envelope['permission_records'][0]['message']['params']['toolCall']['rawInput']['prompt'].endswith('-')


@pytest.mark.parametrize('ending', ['error', 'cancel'])
def test_permission_writer_failure_preserves_original_exception_D23(ending):
    async def scenario():
        original = ValueError('original failure') if ending == 'error' else asyncio.CancelledError()
        def failed_writer(*args, **kwargs):
            raise OSError('synthetic staging failure')
        router = worker().TurnObserver(observer(), backend='qwen-code', provenance={},
            identity=lambda: {}, scrub=deepcopy, write_log=failed_writer)
        async def turn(**kwargs):
            router.observe('receive', rpc(1))
            raise original
        with pytest.raises(type(original)) as caught:
            await router.wrap(turn)(backend='qwen-code')
        assert caught.value is original and router.current is None
        assert original.__notes__ == ['D23 native observation could not be staged']
    asyncio.run(scenario())


def test_permission_cached_observed_transport_tracks_current_turn_and_restores_hooks_D23():
    from dataclasses import dataclass
    @dataclass
    class Process:
        transport: object
        kill: object
        proc: object
        dispose: object
    @dataclass
    class Profile:
        spawn: object
    async def scenario():
        module, capture, logs, sent, received, closed = worker(), observer(), [], [], [], []
        class OriginalTransport:
            async def send(self, message):
                sent.append(message)
            async def receive(self):
                return received.pop(0)
            async def close(self):
                closed.append(True)
        process = Process(OriginalTransport(), object(), object(), object())
        spawn_count = 0
        async def spawn():
            nonlocal spawn_count
            spawn_count += 1
            return process
        async def prepare(context):
            assert context == {'original': True}
            return Profile(spawn)
        acp = SimpleNamespace(_PROFILES={'qwen-code': prepare})
        cached = None
        async def original_turn(**kwargs):
            nonlocal cached
            if cached is None:
                cached = await (await acp._PROFILES['qwen-code']({'original': True})).spawn()
                assert (cached.kill, cached.proc, cached.dispose) == (process.kill, process.proc, process.dispose)
            number = len(logs) + 1
            await cached.transport.send({'id': number, 'method': 'session/prompt',
                'params': {'sessionId': 'native', 'prompt': 'unretained'}})
            request = rpc(1)  # Peer ID reuse across different turns must remain disjoint.
            received.append(request)
            assert await cached.transport.receive() is request
            reply = {'id': 1, 'result': {'outcome': {'outcome': 'selected', 'optionId': 'allow'}}}
            await cached.transport.send(reply)
            response = {'id': number, 'result': {'stopReason': 'end_turn'}}
            received.append(response)
            assert await cached.transport.receive() is response
        async def run(*, here):
            assert here == '/accepted/agent'
            try:
                await session.entry.run_agent(backend='qwen-code')
                await session.entry.run_agent(backend='qwen-code')
                return 'original result'
            finally:
                await cached.transport.close()
        session = SimpleNamespace(run=run, entry=SimpleNamespace(run_agent=original_turn))
        assert await module.run_observed(session, '/accepted/agent', acp, capture,
            {'backend': 'qwen-code'}, identity=lambda: {}, scrub=deepcopy,
            write_log=lambda path, body, **kwargs: logs.append(json.loads(body))) == 'original result'
        assert session.entry.run_agent is original_turn and acp._PROFILES['qwen-code'] is prepare
        assert spawn_count == 1 and closed == [True] and len(sent) == 4
        assert [body['records'][0]['id'] for body in logs] == [1, 2]
        assert all(len(body['permission_records']) == 2 for body in logs)
        assert all(body['permission_records'][0]['message'] == rpc(1) for body in logs)
    asyncio.run(scenario())
