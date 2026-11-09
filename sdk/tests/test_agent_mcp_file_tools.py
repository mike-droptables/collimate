"""P4F26: MCP file creation never depends on a harness's native tool roster."""
import asyncio
import hashlib
import json

import httpx
import pytest

from _agent_shared import mcp_bridge, workspace_tools  # noqa: E402


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_mcp_file_tools_are_the_enabled_file_rows_P4F26(commands):
    files = {row[0] for row in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=False, commands=commands) if row[1] == 'file'}
    assert files == {'list_files', 'read_file', 'grep', 'view_image', 'write_file', 'edit_file',
                     'read_newer_version', 'accept_newer_version'}
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


def _refused(bridge, path, revision):
    """The bridge's record of a write of ``path`` a newer revision refused."""
    bridge._persistence['conflicts'][path] = {'revision': revision, 'writer': {'kind': 'user'},
                                              'candidates': ['candidate-1'], 'body_hash': None}


def test_the_mcp_write_tools_take_replace_revision_only_naming_a_refused_revision(native_bridge, tmp_path):
    """The override is a parameter of the MCP write tools (ACP fs/write has
    none): optional, an integer, and refused unless it names the revision a
    blocked write of that path reported."""
    async def run():
        rows = workspace_tools.tool_entries(enable_file_tools=True, websearch=False, commands='deny')
        bridge = mcp_bridge.start_bridge(loop=asyncio.get_running_loop(),
                                        bind_host='127.0.0.1', extra_entries=rows)
        try:
            for _ in range(200):
                if bridge._server.started:
                    break
                await asyncio.sleep(.01)
            async with httpx.AsyncClient(timeout=5) as client:
                async def call(method, **params):
                    response = await client.post(bridge.url_host, headers={
                        'Accept': 'application/json, text/event-stream'},
                        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                    message = (json.loads(next(line[6:] for line in response.text.splitlines()
                               if line.startswith('data: ')))
                               if response.headers['content-type'].startswith('text/event-stream')
                               else response.json())
                    return message['result']
                tools = {tool['name']: tool for tool in (await call('tools/list'))['tools']}
                for name in ('write_file', 'edit_file'):
                    schema = tools[name]['inputSchema']
                    assert 'replace_revision' in schema['properties']
                    assert 'replace_revision' not in schema.get('required', [])
                    assert 'integer' in json.dumps(schema['properties']['replace_revision'])
                    assert 'replace_revision' in tools[name]['description']
                # Reading or accepting the newer version names its revision too.
                for name in ('read_newer_version', 'accept_newer_version'):
                    schema = tools[name]['inputSchema']
                    assert {'path', 'revision'} <= set(schema['required'])
                    assert 'integer' in json.dumps(schema['properties']['revision'])
                eager = await call('tools/call', name='write_file', arguments={
                    'path': 'a.md', 'content': 'mine', 'replace_revision': 3})
                assert 'no refused write' in str(eager)
                assert not (tmp_path / 'a.md').exists() and not native_bridge.replacements
                _refused(native_bridge, 'a.md', 3)
                wrong = await call('tools/call', name='write_file', arguments={
                    'path': 'a.md', 'content': 'mine', 'replace_revision': 2})
                assert 'named revision 3, not 2' in str(wrong) and not native_bridge.replacements
                named = await call('tools/call', name='write_file', arguments={
                    'path': 'a.md', 'content': 'mine', 'replace_revision': 3})
                assert 'replacing revision 3 of a.md deliberately' in str(named), named
                assert native_bridge.replacements == {'a.md': 3}
                assert (tmp_path / 'a.md').read_text() == 'mine'
                edited = await call('tools/call', name='edit_file', arguments={
                    'path': 'a.md', 'old': 'mine', 'new': 'mine, again', 'replace_revision': 3})
                assert 'replacing revision 3' in str(edited), edited
                plain = await call('tools/call', name='edit_file', arguments={
                    'path': 'a.md', 'old': 'again', 'new': 'once more'})
                assert str(plain).count('replacing') == 0
        finally:
            await asyncio.to_thread(bridge.stop)
    asyncio.run(run())


def test_the_newer_version_tools_read_and_accept_only_the_named_revision(native_bridge, tmp_path):
    """A model can read all of a newer version that blocked its write (any
    size, paged like read_file) and accept it, but only by naming the
    revision the block reported; reading never changes its workspace copy."""
    big = ''.join(f'their line {i}\n' for i in range(900))
    native_bridge.read_cell_files = lambda **kw: (
        {'a.md': big} if kw == {'paths': ['a.md'], 'revisions': {'a.md': 3}, 'observe': False} else {})
    (tmp_path / 'a.md').write_text('mine\n')
    read, accept = workspace_tools.read_newer_version, workspace_tools.accept_newer_version
    assert 'no blocked write of a.md' in asyncio.run(read('a.md', 3))
    assert 'no blocked write of a.md' in asyncio.run(accept('a.md', 3))
    native_bridge._persistence['conflicts']['a.md'] = {
        'revision': 3, 'writer': {'kind': 'user'}, 'candidates': ['candidate-1'],
        'body_hash': hashlib.sha256(big.encode()).hexdigest()}
    assert 'named revision 3, not 2' in asyncio.run(read('a.md', 2))
    page = asyncio.run(read('a.md', 3, offset=850, limit=100))
    assert page.splitlines()[0] == '   851\ttheir line 850' and page.splitlines()[-1] == '   900\ttheir line 899'
    assert (tmp_path / 'a.md').read_text() == 'mine\n'
    assert native_bridge._persistence['revisions'].get('a.md', 0) == 0
    assert 'named revision 3, not 4' in asyncio.run(accept('a.md', 4))
    assert 'accepted revision 3 of a.md' in asyncio.run(accept('a.md', 3))
    assert (tmp_path / 'a.md').read_text() == big and 'a.md' in native_bridge.tracked_writes
    assert not native_bridge.replacements

