"""P4F26: MCP file creation never depends on a harness's native tool roster."""
import asyncio
import json

import httpx
import pytest

from _agent_shared import mcp_bridge, workspace_tools  # noqa: E402


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_mcp_file_tools_are_the_enabled_file_rows_P4F26(commands):
    files = {row[0] for row in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=False, commands=commands) if row[1] == 'file'}
    assert files == {'list_files', 'read_file', 'grep', 'view_image', 'write_file', 'edit_file'}
    assert not {row[0] for row in workspace_tools.tool_entries(
        enable_file_tools=False, websearch=False, commands=commands) if row[1] == 'file'}


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('modern', [True, False])
def test_actual_mcp_file_creation_read_and_commands_deny_P4F26(native_bridge, tmp_path, enabled, modern):
    """Real HTTP server and SDK host filesystem bridge; no model response substitute."""
    native_bridge.permissions = {'commands': 'deny', 'internet': 'none', 'docker': 'none'}
    filename, body = 'opening-comment.txt', 'actual-host-file-marker'
    assert not (tmp_path / filename).exists()
    async def run():
        rows = workspace_tools.tool_entries(enable_file_tools=enabled, websearch=False,
                                           commands='deny')
        bridge = mcp_bridge.start_bridge(loop=asyncio.get_running_loop(),
                                        bind_host='127.0.0.1', extra_entries=rows)
        assert bridge is not None
        try:
            for _ in range(200):
                if bridge._server.started:
                    break
                await asyncio.sleep(.01)
            assert bridge._server.started
            async with httpx.AsyncClient(timeout=5) as client:
                async def call(method, **params):
                    headers = {'Accept': 'application/json, text/event-stream'}
                    if modern:
                        version = '2026-07-28'
                        headers.update({'MCP-Protocol-Version': version, 'MCP-Method': method})
                        if 'name' in params:
                            headers['MCP-Name'] = params['name']
                        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': version,
                            'io.modelcontextprotocol/clientCapabilities': {},
                            'io.modelcontextprotocol/clientInfo': {'name': 'file-proof', 'version': '1'}}
                    response = await client.post(bridge.url_host, headers=headers,
                        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                    assert response.status_code == 200, response.text
                    message = (json.loads(next(line[6:] for line in response.text.splitlines()
                               if line.startswith('data: ')))
                               if response.headers['content-type'].startswith('text/event-stream')
                               else response.json())
                    assert 'error' not in message, message
                    return message['result']
                # Call the actual registered writer, rather than assuming a list
                # containing its name means the implementation is usable.
                written = await call('tools/call', name='write_file',
                                     arguments={'path': filename, 'content': body})
                if enabled:
                    assert not written.get('isError', False), written
                    assert (tmp_path / filename).read_text() == body
                    read = await call('tools/call', name='read_file', arguments={'path': filename})
                    assert not read.get('isError', False) and body in str(read), read
                else:
                    assert written.get('isError') is True, written
                    assert not (tmp_path / filename).exists()
                listed = await call('tools/list')
                names = {tool['name'] for tool in listed['tools']}
                assert ('read_file' in names) is enabled
                assert ('write_file' in names) is enabled
                assert not names & {'run_bash', 'run_python', 'test_generator'}
                refused = await call('tools/call', name='run_bash',
                                     arguments={'command': 'touch forbidden-command.txt'})
                assert refused.get('isError') is True, refused
                assert not (tmp_path / 'forbidden-command.txt').exists()
        finally:
            await asyncio.to_thread(bridge.stop)
        assert not bridge._thread.is_alive()
    asyncio.run(run())
