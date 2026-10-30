"""P4F25: actual native-era HTTP tool traffic, capability and grant boundaries."""
import asyncio
import base64
import json

import httpx
import pytest

from _agent_shared import mcp_bridge, workspace_tools  # noqa: E402

VERSION = '2026-07-28'


def request(method, **params):
    return {'jsonrpc': '2.0', 'id': 7, 'method': method, 'params': {
        **params, '_meta': {
            'io.modelcontextprotocol/protocolVersion': VERSION,
            'io.modelcontextprotocol/clientCapabilities': {},
            'io.modelcontextprotocol/clientInfo': {'name': 'native-proof', 'version': '1'},
        }}}


def result(response):
    assert response.status_code == 200, response.text
    if response.headers['content-type'].startswith('text/event-stream'):
        data = next(line[6:] for line in response.text.splitlines() if line.startswith('data: '))
        body = json.loads(data)
    else:
        body = response.json()
    assert body['id'] == 7 and 'error' not in body, body
    return body['result']


def test_modern_native_http_discover_list_call_and_stop_P4F25(native_bridge):
    """No client-version fallback: reproduce the exact rejected native header."""
    async def run():
        rows = workspace_tools.tool_entries(enable_file_tools=False, websearch=False,
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
                async def post(method, **params):
                    return await client.post(bridge.url_host, json=request(method, **params),
                        headers={'Accept': 'application/json, text/event-stream',
                                 'MCP-Protocol-Version': VERSION, 'MCP-Method': method,
                                 **({'MCP-Name': params['name']} if 'name' in params else {})})
                discovery = result(await post('server/discover'))
                assert VERSION in discovery['supportedVersions']
                listed = result(await post('tools/list'))
                assert [tool['name'] for tool in listed['tools']] == [
                    'list_handlers', 'handler_skills', 'validate_file']
                # Expected local input failure stays a valid tool response.
                called = result(await post('tools/call', name='validate_file',
                                           arguments={'path': 'missing.note'}))
                assert not called.get('isError', False)
                assert 'valid' in str(called) and 'False' in str(called)
                assert 'Traceback' not in str(called)
                # A caller cannot obtain a tool withheld by this run's grant.
                refused = result(await post('tools/call', name='test_generator', arguments={}))
                assert refused['isError'] is True
                bad_host = await client.post(bridge.url_host, json=request('server/discover'),
                    headers={'Host': 'foreign.invalid', 'Accept': 'application/json, text/event-stream',
                             'MCP-Protocol-Version': VERSION, 'MCP-Method': 'server/discover'})
                assert bad_host.status_code == 421
                bad_origin = await client.post(bridge.url_host, json=request('server/discover'),
                    headers={'Origin': 'https://foreign.invalid',
                             'Accept': 'application/json, text/event-stream',
                             'MCP-Protocol-Version': VERSION, 'MCP-Method': 'server/discover'})
                assert bad_origin.status_code == 403
                for path in ('/mcp', '/wrong/mcp'):
                    response = await client.post(f'http://127.0.0.1:{bridge.port}{path}',
                                                 json=request('tools/list'))
                    assert response.status_code == 404
        finally:
            await asyncio.to_thread(bridge.stop)
        assert not bridge._thread.is_alive()
        async with httpx.AsyncClient(timeout=.5) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(bridge.url_host, json=request('tools/list'))
    asyncio.run(run())


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('extension,media_type', [('png', 'image/png'), ('jpg', 'image/jpeg')])
def test_actual_mcp_image_is_image_content_P4F25(native_bridge, modern, extension, media_type):
    """The bridge's native content helper must carry image bytes on the wire."""
    import gen
    pixels = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2v8AAAAASUVORK5CYII=')
    # The file tool's media contract uses the filename and preserves bytes;
    # decoding/re-encoding is outside this path.
    filename = f'pixel.{extension}'
    gen.update_file(filename, pixels)
    async def run():
        rows = workspace_tools.tool_entries(enable_file_tools=True, websearch=False,
                                           commands='deny')
        bridge = mcp_bridge.start_bridge(loop=asyncio.get_running_loop(),
                                        bind_host='127.0.0.1', extra_entries=rows)
        assert bridge is not None
        try:
            for _ in range(200):
                if bridge._server.started:
                    break
                await asyncio.sleep(.01)
            async with httpx.AsyncClient(timeout=5) as client:
                params = {'name': 'view_image', 'arguments': {'path': filename}}
                payload = (request('tools/call', **params) if modern else
                           {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': params})
                response = await client.post(bridge.url_host,
                    headers={'Accept': 'application/json, text/event-stream',
                             **({'MCP-Protocol-Version': VERSION, 'MCP-Method': 'tools/call',
                                 'MCP-Name': 'view_image'} if modern else {})}, json=payload)
                contents = result(response)['content']
                assert len(contents) == 1 and contents[0]['type'] == 'image', contents
                assert contents[0]['mimeType'] == media_type
                assert base64.b64decode(contents[0]['data']) == pixels
        finally:
            await asyncio.to_thread(bridge.stop)
    asyncio.run(run())
