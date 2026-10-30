"""Unpaid native Qwen ACP MCP admission diagnostics; never sends a model prompt."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
import threading
import time

import pytest

from _agent_shared import acp_run
from tests_live.conftest import (
    bind_model_account, has_key, model_account, model_for, requires_docker,
    run_on_test_loop,
)

pytestmark = [pytest.mark.live, requires_docker,
              pytest.mark.usefixtures('one_loop_per_test'),
              pytest.mark.skipif(not has_key('openrouter'),
                                 reason='needs normal selected OpenRouter account setup')]

PINS = {'qwen-code': '0.26.1'}
SERVER = 'collimate'
TOOL = 'test_generator'
MAX_EVIDENCE_BYTES = 256 * 1024


def status_errors(value):
    """Classify known native errors; never retain free text or capability URLs."""
    errors = value.get('errors', [])
    assert isinstance(errors, list) and len(errors) <= 32
    result = []
    known = {f'MCP server not configured: {SERVER}': 'server_not_configured',
             'Tool registry is not initialized.': 'registry_uninitialized'}
    for error in errors:
        assert isinstance(error, dict)
        text = error.get('error')
        assert text is None or isinstance(text, str)
        raw = (text or '').encode()
        assert len(raw) <= 16384, 'Native status error exceeded diagnostic bound'
        result.append({'reason': known.get(text, 'unknown'),
                       'message_bytes': len(raw), 'message_sha256': hashlib.sha256(raw).hexdigest()})
    return result


def readiness_summary(resources, tools, builtins, acp_session_id):
    """Session discovery and optional workspace diagnostics have separate authority."""
    assert isinstance(resources, dict) and resources.get('sessionId') == acp_session_id
    assert resources.get('workspaceCwd') == '/workspace'
    mcp = resources.get('mcp')
    assert isinstance(mcp, dict) and isinstance(mcp.get('servers'), list)
    servers = [{key: row[key] for key in (
        'name', 'status', 'mcpStatus', 'transport', 'disabled', 'approvalState',
        'requiresAuth', 'toolCount', 'errorKind') if key in row}
        for row in mcp['servers'] if row.get('name') == SERVER]
    summary = {'discovery_state': mcp.get('discoveryState'), 'servers': servers,
               'errors': status_errors(mcp), 'model_request_visibility': 'not_observed'}
    if tools is not None or builtins is not None:
        assert isinstance(tools, dict) and tools.get('serverName') == SERVER
        assert isinstance(tools.get('tools'), list)
        assert isinstance(builtins, dict) and isinstance(builtins.get('tools'), list)
        summary['workspace'] = {
            'mcp_tools': {'count': len(tools['tools']), 'errors': status_errors(tools)},
            'builtin_tools': {'count': len(builtins['tools']), 'errors': status_errors(builtins)}}
    return summary


def is_ready(snapshot):
    servers = snapshot['servers']
    return (not snapshot['errors'] and snapshot['discovery_state'] == 'completed'
            and len(servers) == 1 and servers[0].get('mcpStatus') == 'connected'
            and servers[0].get('status') == 'ok' and servers[0].get('disabled') is False)


def context_summary(context, acp_session_id):
    """Native nonhidden registry names, never provider request visibility/content."""
    assert isinstance(context, dict) and context.get('sessionId') == acp_session_id
    assert context.get('workspaceCwd') == '/workspace'
    usage = context.get('usage')
    assert isinstance(usage, dict) and usage.get('showDetails') is True
    assert isinstance(usage.get('isEstimated'), bool)
    names = {}
    for field in ('builtinTools', 'mcpTools'):
        rows = usage.get(field)
        assert isinstance(rows, list) and len(rows) <= 1024
        assert all(isinstance(row, dict) and isinstance(row.get('name'), str)
                   and 0 < len(row['name']) <= 256 for row in rows)
        names[field] = [row['name'] for row in rows]
    return {'builtin_names': names['builtinTools'], 'mcp_names': names['mcpTools'],
            'selected_nonhidden': f'{SERVER}__{TOOL}' in names['mcpTools'],
            'tool_search_nonhidden': 'tool_search' in names['builtinTools'],
            'is_estimated': usage['isEstimated'],
            'scope': 'current_session_nonhidden_registry',
            'empty_is_inconclusive': not names['builtinTools'] and not names['mcpTools'],
            'model_request_visibility': 'not_observed'}


async def poll_session_resources(session, record):
    deadline = time.monotonic() + 30
    # One status request per poll fits the unchanged256-wire bound even if
    # discovery never completes. Workspace diagnostics cannot prolong it.
    for _ in range(151):
        # Python ext_method prefixes "_"; Qwen dispatches literal names.
        resources = await asyncio.wait_for(session.conn._conn.send_request(
            'qwen/status/session/resources', {'sessionId': session.acp_session_id}), 10)
        snapshot = readiness_summary(resources, None, None, session.acp_session_id)
        record(snapshot, 'snapshots')
        if is_ready(snapshot):
            return resources
        assert time.monotonic() < deadline, 'Native session MCP did not become ready within30seconds'
        await asyncio.sleep(.2)
    raise AssertionError('Native session MCP did not become ready within151polls')


class AdmissionTransport:
    """Delegate the actual transport unchanged except refusing any model prompt."""
    def __init__(self, transport, record):
        self.transport, self.record = transport, record

    async def send(self, message):
        method = message.get('method') if isinstance(message, dict) else None
        if method == 'session/prompt':
            raise AssertionError('Unpaid readiness probe must never send session/prompt')
        if method:
            item = {'direction': 'send', 'method': method}
            if method == 'session/new':
                params = message.get('params', {})
                item['cwd'] = params.get('cwd')
                item['mcp_servers'] = [
                    {'name': server.get('name'), 'transport': server.get('type', 'stdio')}
                    for server in params.get('mcpServers', [])]
            self.record(item)
        return await self.transport.send(message)

    async def receive(self):
        message = await self.transport.receive()
        if isinstance(message, dict):
            result = message.get('result')
            if isinstance(result, dict) and 'agentCapabilities' in result:
                caps = result['agentCapabilities']
                self.record({'direction': 'receive', 'mcp_capabilities': caps.get('mcpCapabilities')})
        return message

    async def close(self):
        return await self.transport.close()


def test_actual_qwen_acp_mcp_readiness_without_model_D23(docker_env_bridge, monkeypatch, tmp_path):
    """Same selected account/prepare/session; no prompt, model call or tool execution."""
    bridge = docker_env_bridge
    bridge.channel = 'p4-qwen-unpaid-acp-readiness'
    bridge.cell_id = 'p4-qwen-unpaid-acp-readiness-cell'
    bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': 'allow'}
    bridge.packages = dict(PINS)
    model = model_for('openrouter')
    bind_model_account(model_account('openrouter', model=model, harness='qwen-code'))
    selected_key = os.environ['OPENROUTER_API_KEY']
    start = time.monotonic()
    evidence = {'pins': PINS, 'model': model, 'wire': [], 'discoveries': [], 'snapshots': [],
                'diagnostics': []}
    lock = threading.Lock()

    def record(item, bucket='wire'):
        item = {'elapsed_ms': round((time.monotonic() - start) * 1000), **item}
        # Fail closed before storing accidental selected credential/capability data.
        text = json.dumps(item)
        assert selected_key not in text, 'Selected credential in diagnostic evidence'
        with lock:
            assert len(evidence[bucket]) < 256, 'Readiness diagnostic record bound exceeded'
            candidate = {**evidence, bucket: [*evidence[bucket], item]}
            assert len(json.dumps(candidate, indent=2).encode()) <= MAX_EVIDENCE_BYTES, 'Readiness byte bound exceeded'
            evidence[bucket].append(item)

    original_profile = acp_run._PROFILES['qwen-code']
    async def prepare(context):
        profile = await original_profile(context)
        async def spawn():
            process = await profile.spawn()
            return replace(process, transport=AdmissionTransport(process.transport, record))
        return replace(profile, spawn=spawn)
    monkeypatch.setitem(acp_run._PROFILES, 'qwen-code', prepare)

    from mcp.server.mcpserver import MCPServer
    original_list = MCPServer.list_tools
    async def observe_discovery(server):
        tools = await original_list(server)
        record({'tool_names': sorted(tool.name for tool in tools),
                'selected_schema': next((tool.input_schema for tool in tools if tool.name == TOOL), None)},
               'discoveries')
        return tools
    monkeypatch.setattr(MCPServer, 'list_tools', observe_discovery)
    async def forbidden_call(*args, **kwargs):
        record({'blocked_tool_call': True})
        raise AssertionError('Readiness probe must not execute an MCP tool')
    monkeypatch.setattr(MCPServer, 'call_tool', forbidden_call)

    async def run():
        session = None
        try:
            session = await acp_run._open_session(
                backend='qwen-code', auth='api_key', provider='openrouter', model=model,
                system_prompt='', mcp_decls=[], enable_file_tools=False, websearch=False,
                turn_skills=None, approvals='auto', commands='allow')
            assert session.conn is not None
            evidence['acp_session_id'] = session.acp_session_id
            evidence['declared_server_names'] = list(session.mcp_server_names)
            assert evidence['declared_server_names'] == [SERVER], 'Unexpected native server declarations'
            resources = await poll_session_resources(session, record)
            tools = await asyncio.wait_for(session.conn._conn.send_request(
                'qwen/status/workspace/mcp/tools', {'serverName': SERVER}), 10)
            builtins = await asyncio.wait_for(session.conn._conn.send_request(
                'qwen/status/workspace/tools', {}), 10)
            record(readiness_summary(resources, tools, builtins, session.acp_session_id), 'diagnostics')
            context = await asyncio.wait_for(session.conn._conn.send_request(
                'qwen/status/session/context_usage', {'sessionId': session.acp_session_id, 'detail': True}), 10)
            record(context_summary(context, session.acp_session_id), 'diagnostics')
            assert any(TOOL in row['tool_names'] and isinstance(row['selected_schema'], dict)
                       for row in evidence['discoveries']), (
                'No genuine native tools/list reached the original MCP server')
            assert not any(row.get('method') == 'session/prompt' or row.get('blocked_tool_call')
                           for row in evidence['wire']), 'Unexpected prompt/tool execution attempt'
            # Diagnostic completion proves discovery only. An absent selected
            # nonhidden tool is retained for diagnosis, not recast as delivery.
            evidence['session_discovery_passed'] = True
        except BaseException as error:
            evidence['failure_type'] = type(error).__name__
            raise
        finally:
            if session is not None:
                await session.close()
                evidence['owned_session_closed'] = session.conn is None and session.adapter is None

    try:
        run_on_test_loop(asyncio.wait_for(run(), timeout=180))
    finally:
        # The fixture closes run-owned bridges/containers after this own session.
        # Only whitelisted config-free fields are written, never native settings.
        body = json.dumps(evidence, indent=2).encode()
        assert selected_key.encode() not in body, 'Selected credential in diagnostic evidence'
        assert len(body) <= MAX_EVIDENCE_BYTES, 'Readiness final byte bound exceeded'
        fd = os.open(tmp_path / 'qwen-acp-readiness.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
