"""Actual installed MCP Tool representation at the unpaid native fixture boundary."""
import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError
from mcp.server.mcpserver import MCPServer

SOURCE = Path(__file__).resolve().parents[1] / 'tests_live/test_live_qwen_acp_readiness.py'


def status_helpers():
    """Load pure live summaries without Docker/account fixture imports."""
    tree = ast.parse(SOURCE.read_text())
    names = {'readiness_summary', 'is_ready', 'status_errors', 'context_summary',
             'poll_session_resources', 'AdmissionTransport'}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    namespace = {'SERVER': 'collimate', 'TOOL': 'test_generator',
                 'hashlib': __import__('hashlib'), 'asyncio': asyncio, 'time': __import__('time')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


def session_resources():
    return {'sessionId': 'owned-acp-session', 'workspaceCwd': '/workspace',
            'mcp': {'discoveryState': 'completed', 'servers': [
                {'name': 'collimate', 'status': 'ok', 'mcpStatus': 'connected',
                 'transport': 'http', 'disabled': False}]}}


def test_qwen_session_discovery_is_not_workspace_server_readiness_D23():
    tools = {'serverName': 'collimate', 'tools': [], 'errors': [
        {'kind': 'mcp_tools', 'status': 'error', 'error': 'MCP server not configured: collimate'}]}
    builtins = {'tools': [], 'errors': [
        {'kind': 'tools', 'status': 'error', 'error': 'Tool registry is not initialized.'}]}
    helpers = status_helpers()
    summary = helpers['readiness_summary'](session_resources(), tools, builtins, 'owned-acp-session')
    assert helpers['is_ready'](summary), 'Workspace-only errors must not keep a connected session polling'
    assert summary['workspace']['mcp_tools']['errors'][0]['reason'] == 'server_not_configured'
    assert summary['workspace']['builtin_tools']['errors'][0]['reason'] == 'registry_uninitialized'


def discovery_observer(original, record):
    # Exercise the actual nested live observer without importing/gating native
    # fixtures or starting their Docker/account/session dependencies.
    tree = ast.parse(SOURCE.read_text())
    live = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'test_actual_qwen_acp_mcp_readiness_without_model_D23')
    callback = next(node for node in live.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'observe_discovery')
    namespace = {'original_list': original, 'record': record, 'TOOL': 'test_generator'}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['observe_discovery']


def test_qwen_readiness_observer_keeps_actual_official_mcp_schema_and_return_D23():
    async def run():
        server = MCPServer('synthetic-readiness')
        executed = []

        @server.tool(name='test_generator')
        def generator(path: str, settings: dict, prompt: str) -> str:
            executed.append(True)
            raise AssertionError('Schema discovery must never execute the tool')

        @server.tool(name='other')
        def unrelated() -> str:
            raise AssertionError('Schema discovery must never execute the tool')

        original_results = []
        async def original(actual_server):
            assert actual_server is server
            result = await MCPServer.list_tools(actual_server)
            original_results.append(result)
            return result
        records = []
        def record(value, bucket):
            records.append((bucket, value))
        result = await discovery_observer(original, record)(server)
        assert result is original_results[0], 'Observer must return the actual original list'
        selected = next(tool for tool in result if tool.name == 'test_generator')
        assert records == [('discoveries', {'tool_names': ['other', 'test_generator'],
                                           'selected_schema': selected.input_schema})]
        assert set(selected.input_schema['properties']) == {'path', 'settings', 'prompt'}
        assert set(selected.input_schema['required']) == {'path', 'settings', 'prompt'}
        assert executed == []
    asyncio.run(run())


def test_qwen_readiness_original_discovery_failure_is_not_false_evidence_D23():
    async def run():
        records = []
        async def original(server):
            raise RuntimeError('synthetic original discovery failure')
        with pytest.raises(RuntimeError, match='synthetic original discovery failure'):
            await discovery_observer(original, lambda *args: records.append(args))(object())
        assert records == []
    asyncio.run(run())


def native_status_request(result_name):
    """Execute the live fixture's real await expression on the installed connection."""
    tree = ast.parse(SOURCE.read_text())
    query = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == result_name
                         for target in node.targets)
                 and any(isinstance(part, ast.Attribute) and part.attr in ('ext_method', 'send_request')
                         for part in ast.walk(node.value)))
    wrapper = ast.parse('async def request(session):\n    return None\n').body[0]
    wrapper.body = [ast.Return(value=query.value)]
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    namespace = {'asyncio': asyncio, 'SERVER': 'collimate'}
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['request']


class StatusTransport:
    """Queue transport for real ACP request IDs, receive routing and lifetime."""
    def __init__(self):
        self.outgoing = asyncio.Queue()
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, message):
        await self.outgoing.put(message)

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(('result_name', 'method', 'params'), [
    ('resources', 'qwen/status/session/resources', {'sessionId': 'owned-acp-session'}),
    ('tools', 'qwen/status/workspace/mcp/tools', {'serverName': 'collimate'}),
    ('builtins', 'qwen/status/workspace/tools', {}),
    ('context', 'qwen/status/session/context_usage', {'sessionId': 'owned-acp-session', 'detail': True}),
])
def test_qwen_native_status_exact_method_real_connection_D23(result_name, method, params):
    async def run():
        transport = StatusTransport()
        observed = []
        conn = ClientSideConnection(object(), transport, observers=[observed.append])
        session = SimpleNamespace(conn=conn, acp_session_id='owned-acp-session')
        task = asyncio.create_task(native_status_request(result_name)(session))
        try:
            sent = await asyncio.wait_for(transport.outgoing.get(), 1)
            assert sent == {'jsonrpc': '2.0', 'id': 0, 'method': method, 'params': params}
            result = {'native-status': result_name}
            await transport.incoming.put({'jsonrpc': '2.0', 'id': sent['id'], 'result': result})
            assert await asyncio.wait_for(task, 1) == result
            assert [(event.direction.value, event.message) for event in observed] == [
                ('outgoing', sent),
                ('incoming', {'jsonrpc': '2.0', 'id': 0, 'result': result}),
            ]
            assert conn._conn._pending == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await conn.close()
        assert transport.closed
    asyncio.run(run())


def test_qwen_native_status_preserves_real_remote_error_D23():
    async def run():
        transport = StatusTransport()
        conn = ClientSideConnection(object(), transport)
        session = SimpleNamespace(conn=conn, acp_session_id='owned-acp-session')
        task = asyncio.create_task(native_status_request('resources')(session))
        try:
            sent = await asyncio.wait_for(transport.outgoing.get(), 1)
            await transport.incoming.put({'jsonrpc': '2.0', 'id': sent['id'], 'error': {
                'code': -32602, 'message': 'synthetic invalid owned session'}})
            with pytest.raises(RequestError, match='synthetic invalid owned session') as caught:
                await asyncio.wait_for(task, 1)
            assert caught.value.code == -32602
            assert conn._conn._pending == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await conn.close()
        assert transport.closed
    asyncio.run(run())


def test_qwen_native_status_preserves_cancel_reuse_ids_and_close_D23():
    async def run():
        transport = StatusTransport()
        conn = ClientSideConnection(object(), transport)
        session = SimpleNamespace(conn=conn, acp_session_id='owned-acp-session')
        tasks = []
        try:
            first = asyncio.create_task(native_status_request('resources')(session))
            tasks.append(first)
            first_sent = await asyncio.wait_for(transport.outgoing.get(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert conn._conn._pending == {}

            second = asyncio.create_task(native_status_request('tools')(session))
            tasks.append(second)
            second_sent = await asyncio.wait_for(transport.outgoing.get(), 1)
            assert second_sent['id'] == first_sent['id'] + 1
            await transport.incoming.put({'jsonrpc': '2.0', 'id': second_sent['id'],
                                          'result': {'tools': []}})
            assert await asyncio.wait_for(second, 1) == {'tools': []}
            assert conn._conn._pending == {}

            third = asyncio.create_task(native_status_request('builtins')(session))
            tasks.append(third)
            third_sent = await asyncio.wait_for(transport.outgoing.get(), 1)
            assert third_sent['id'] == second_sent['id'] + 1
            await conn.close()
            with pytest.raises(ConnectionError, match='Connection closed'):
                await asyncio.wait_for(third, 1)
            assert conn._conn._pending == {}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await conn.close()
        assert transport.closed
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['session', 'workspace', 'discovery', 'disabled', 'disconnected',
                                  'error', 'status', 'duplicate'])
def test_qwen_session_readiness_remains_strict_D23(damage):
    resources = session_resources()
    if damage == 'session': resources['sessionId'] = 'other'
    elif damage == 'workspace': resources['workspaceCwd'] = '/other'
    elif damage == 'discovery': resources['mcp']['discoveryState'] = 'in_progress'
    elif damage == 'disabled': resources['mcp']['servers'][0]['disabled'] = True
    elif damage == 'disconnected': resources['mcp']['servers'][0]['mcpStatus'] = 'disconnected'
    elif damage == 'error': resources['mcp']['errors'] = [{'error': 'synthetic session failure'}]
    elif damage == 'status': resources['mcp']['servers'][0]['status'] = 'error'
    elif damage == 'duplicate': resources['mcp']['servers'] *= 2
    helpers = status_helpers()
    if damage in ('session', 'workspace'):
        with pytest.raises(AssertionError):
            helpers['readiness_summary'](resources, None, None, 'owned-acp-session')
    else:
        assert not helpers['is_ready'](helpers['readiness_summary'](
            resources, None, None, 'owned-acp-session'))


def context_status():
    return {'sessionId': 'owned-acp-session', 'workspaceCwd': '/workspace',
            'formattedText': 'do not retain formatted secret', 'usage': {
                'showDetails': True, 'isEstimated': True,
                'builtinTools': [{'name': 'tool_search', 'tokens': 32}],
                'mcpTools': [{'name': 'collimate__test_generator', 'tokens': 42}],
                'memoryFiles': [{'path': 'private secret path'}],
                'skills': [{'name': 'private secret skill'}]}}


@pytest.mark.parametrize('damage', [None, 'session', 'workspace', 'details', 'missing_names',
                                  'bound', 'empty', 'absent_selected'])
def test_qwen_context_is_only_own_nonhidden_registry_not_provider_visibility_D23(damage):
    value = context_status()
    if damage == 'session': value['sessionId'] = 'other'
    elif damage == 'workspace': value['workspaceCwd'] = '/other'
    elif damage == 'details': value['usage']['showDetails'] = False
    elif damage == 'missing_names': value['usage']['mcpTools'] = [{}]
    elif damage == 'bound': value['usage']['builtinTools'] *= 1025
    elif damage == 'empty': value['usage']['mcpTools'] = value['usage']['builtinTools'] = []
    elif damage == 'absent_selected': value['usage']['mcpTools'] = []
    original = deepcopy(value)
    summarize = status_helpers()['context_summary']
    if damage not in (None, 'empty', 'absent_selected'):
        with pytest.raises(AssertionError):
            summarize(value, 'owned-acp-session')
    else:
        result = summarize(value, 'owned-acp-session')
        assert result['selected_nonhidden'] is (damage is None)
        assert result['tool_search_nonhidden'] is (damage != 'empty')
        assert result['empty_is_inconclusive'] is (damage == 'empty')
        assert result['model_request_visibility'] == 'not_observed'
        assert result['scope'] == 'current_session_nonhidden_registry'
        assert 'secret' not in json.dumps(result)
        assert 'tokens' not in json.dumps(result)
    assert value == original


def test_qwen_status_error_unknown_reason_cannot_publish_free_text_D23():
    helpers = status_helpers()
    errors = helpers['status_errors']({'errors': [
        {'error': 'https://private.invalid/capability?token=synthetic-secret',
         'errorKind': 'also synthetic-secret', 'config': 'synthetic-secret'}]})
    assert errors[0]['reason'] == 'unknown'
    assert errors[0]['message_bytes'] > 0 and len(errors[0]['message_sha256']) == 64
    assert 'synthetic-secret' not in json.dumps(errors)
    with pytest.raises(AssertionError):
        helpers['status_errors']({'errors': [{'error': 'x' * 16385}]})


@pytest.mark.parametrize('ready', [False, True])
def test_qwen_actual_polling_connection_stays_within_original_wire_bound_D23(ready):
    async def run():
        helpers = status_helpers()
        clock = [0.0]
        async def sleep(delay):
            clock[0] += delay
            await asyncio.sleep(0)
        helpers['time'] = SimpleNamespace(monotonic=lambda: clock[0])
        helpers['asyncio'] = SimpleNamespace(wait_for=asyncio.wait_for, sleep=sleep)
        wire, snapshots = [], []
        transport = StatusTransport()
        observed = helpers['AdmissionTransport'](transport, wire.append)
        conn = ClientSideConnection(object(), observed)
        session = SimpleNamespace(conn=conn, acp_session_id='owned-acp-session')
        sent_count = [0]
        async def respond():
            while True:
                sent = await transport.outgoing.get()
                sent_count[0] += 1
                assert sent['method'] == 'qwen/status/session/resources'
                assert sent['params'] == {'sessionId': session.acp_session_id}
                result = session_resources()
                if not ready or sent_count[0] == 1:
                    result['mcp']['discoveryState'] = 'in_progress'
                    result['mcp']['servers'][0]['mcpStatus'] = 'disconnected'
                await transport.incoming.put({'jsonrpc': '2.0', 'id': sent['id'], 'result': result})
        responder = asyncio.create_task(respond())
        def record(value, bucket):
            assert bucket == 'snapshots'
            snapshots.append(value)
        try:
            if ready:
                result = await helpers['poll_session_resources'](session, record)
                assert result == session_resources() and sent_count[0] == 2
            else:
                with pytest.raises(AssertionError, match='within30seconds|within151polls'):
                    await helpers['poll_session_resources'](session, record)
                assert 30 <= clock[0] < 31
                assert sent_count[0] == 151
            assert len(wire) == len(snapshots) == sent_count[0]
            # Setup and three one-shot diagnostics still fit existing256.
            assert len(wire) + 10 < 256
            assert conn._conn._pending == {}
        finally:
            responder.cancel()
            await asyncio.gather(responder, return_exceptions=True)
            await conn.close()
        assert transport.closed
    asyncio.run(run())
