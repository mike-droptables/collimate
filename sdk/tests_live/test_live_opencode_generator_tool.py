# SPDX-License-Identifier: MIT
"""D23 native TTY intake/lifetime; SDK recording publication is not app proof."""
import asyncio
import json
import re
import time
import uuid
from urllib.parse import urlsplit

import gen
import httpx
import pytest

from _agent_shared import cli_container
from _agent_shared.harnesses.opencode import backend as oc
from tests_live.conftest import (bind_model_account, has_key, model_account, model_for,
                                requires_docker, run_on_test_loop)
from tests_live._host_terminal import _host_terminals, _type_line

pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
              pytest.mark.skipif(not has_key('openrouter'), reason='needs authorized OPENROUTER_API_KEY')]


def hosted_terminal_identity(bridge):
    """Recording fixture identity for a real hosted forwarded child."""
    bridge.channel = 'p4-native-opencode'
    bridge.cell_id = 'p4-native-opencode-cell'


def install_forward_recording_ack(bridge, monkeypatch):
    """Commit only this fixture's known ordinary child write, then ACK it."""
    def commit(payload, wait_op, *, timeout=None):
        if (wait_op != 'ack_update' or payload.get('op') != 'update_cell'
                or set(payload) != {'op','artifacts','metadata','operation_id','writer_id','edit_seq','base_revisions','resolves'}):
            raise ValueError('unsupported raw recording operation')
        artifacts = payload["artifacts"]
        if len(artifacts) != 1:
            raise ValueError('unsupported recording workspace artifact')
        artifact = artifacts[0]
        local = str(artifact.get("local_path", ""))
        if (set(artifact) != {"type", "cell_path", "local_path", "encoding"}
                or artifact['encoding'] != 'utf-8'
                or artifact["type"] != "workspace" or artifact["cell_path"] != "native-child.txt"
                or not local.startswith(".collrun/forward-")
                or not local.endswith(f'/persistence/{payload["operation_id"]}/0')):
            raise ValueError('unsupported recording workspace artifact')
        metadata = payload['metadata']
        if (not isinstance(metadata, dict) or set(metadata) - {'name'}
                or metadata.get('name') is not None
                and not isinstance(metadata['name'], str)):
            raise ValueError('unsupported recording metadata')
        version = bridge.update_cell(name=metadata.get('name'), writes=['native-child.txt'])
        return {'op':'ack_update', 'ok':True, 'cell_id':version,
                'operation_id':payload['operation_id'], 'outcome':'accepted',
                'files':[{'path':'native-child.txt','file_revision':bridge._update_counter,
                          'writer_id':payload['writer_id'], 'edit_seq':payload['edit_seq'], 'deleted':False}]}
    monkeypatch.setattr(bridge, 'emit_and_wait', commit)


def terminal_pointer():
    return gen.run_path("terminal/native.terminal")


async def tool_response(url):
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        return await client.post(url, headers={'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})


async def tool_names(url):
    response = await tool_response(url)
    assert response.status_code == 200
    value = (json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
             if response.headers['content-type'].startswith('text/event-stream') else response.json())
    return {row['name'] for row in value['result']['tools']}


async def dead(url):
    try:
        response = await tool_response(url)
    except httpx.ConnectError:
        return
    # A later bridge may reuse the listener port; the old capability route
    # must still be absent. Other HTTP failures do not prove its expiry.
    assert response.status_code == 404


def test_native_opencode_terminal_discovers_calls_and_replaces_bridge_D23(docker_env_bridge, monkeypatch):
    """Real native tool event/child result; first URL dies, replacement is live."""
    hosted_terminal_identity(docker_env_bridge)
    install_forward_recording_ack(docker_env_bridge, monkeypatch)
    docker_env_bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': 'allow'}
    docker_env_bridge.packages = {'opencode': '1.19.4'}
    model = model_for('openrouter')
    bind_model_account(model_account('openrouter', model=model, harness='opencode'))
    terms = _host_terminals(docker_env_bridge, monkeypatch)
    marker = 'd23-native-' + uuid.uuid4().hex
    gen.update_file('draft/probe.generator', 'id: native-probe\nname: Native probe\nsummary: D23 fixture\nentrypoint: main.py\n')
    gen.update_file('draft/main.py', 'import gen\nasync def run():\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n'
        '    await gen.commit()\n'
        f'    print({marker!r})\n'
        'gen.main(run)\n')
    prepared_legs = []
    original_prepare = oc.prepare
    async def recorded_prepare(*args, **kwargs):
        prepared = await original_prepare(*args, **kwargs)
        prepared_legs.append(prepared)
        return prepared
    monkeypatch.setattr(oc, 'prepare', recorded_prepare)
    async def run():
        await gen.commit()
        old_url = old_host_url = None
        for commands in ('allow', 'deny'):
            docker_env_bridge.permissions['commands'] = commands
            cfg = {'provider': 'openrouter', 'model': model, 'auth': 'api_key',
                   'commands': commands, 'websearch': False}
            ready = asyncio.Event()
            ready_facts = {}
            def on_ready(sid, **kwargs):
                ready_facts.update(kwargs)
                ready.set()
            from _agent_shared import surface_runner
            leg = asyncio.create_task(surface_runner('opencode', 'terminal')(
                {**cfg, 'backend': 'opencode'}, label='D23', pointer=terminal_pointer(),
                on_ready=on_ready))
            waiter = asyncio.create_task(ready.wait())
            try:
                await asyncio.wait({leg, waiter}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
                if leg.done():
                    leg.result()
                assert ready.is_set(), 'Native terminal failed to register'
                assert len(prepared_legs) == (1 if commands == 'allow' else 2)
                prepared = prepared_legs[-1]
                before = ready_facts.get('before')
                assert before is not None, 'Native pre-launch session inventory unavailable'
                config = json.loads(await asyncio.to_thread(prepared.env_obj.read_file, oc.OPENCODE_CONFIG_PATH))
                url = config['mcp']['collimate']['url']
                assert url != old_url
                host_url = next(endpoint for endpoint in cli_container.run_bridge_urls()
                                if endpoint != url and urlsplit(endpoint).path == urlsplit(url).path)
                names = await tool_names(host_url)
                assert ('test_generator' in names) is (commands == 'allow')
                if old_url:
                    await dead(old_host_url)
                code, output = await asyncio.to_thread(prepared.env_obj.exec, ['opencode', 'mcp', 'list'])
                assert code == 0 and 'collimate' in output and 'connected' in output.lower(), output
                if commands == 'allow':
                    term = list(terms.values())[-1]
                    await asyncio.to_thread(term.wait_for, b'Ask anything', b'Build', b'OpenCode', timeout=60)
                    await asyncio.to_thread(_type_line, term,
                        'Call collimate_test_generator exactly once with path="draft/probe.generator", '
                        'settings={}, prompt="native-child". Do not run a shell or create output yourself. '
                        'After the tool returns report its result and stop.')
                    deadline = time.monotonic() + 240
                    done = False
                    while time.monotonic() < deadline:
                        ids = await asyncio.to_thread(oc.harness_session_ids, prepared.env_obj)
                        if len(ids - before) == 1:
                            document = await asyncio.to_thread(oc._export_chatlog, prepared.env_obj, next(iter(ids - before)))
                            parts = [part for message in (document or {}).get('messages', [])
                                     for part in message.get('parts', []) if part.get('type') == 'tool']
                            matches = [part for part in parts if part.get('tool', '').endswith('test_generator')
                                       and part.get('state', {}).get('status') == 'completed']
                            if matches:
                                result = matches[-1]['state']
                                assert result['input'] == {'path': 'draft/probe.generator', 'settings': {}, 'prompt': 'native-child'}
                                assert marker in str(result['output'])
                                assert re.search(r'"exit_code"\s*:\s*0(?:[,}])', str(result['output']))
                                assert re.search(r'"success"\s*:\s*true', str(result['output']))
                                assert gen.read_file('native-child.txt') == marker
                                assert docker_env_bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
                                done = True
                                break
                        await asyncio.sleep(1)
                    assert done, 'Actual native TTY did not dispatch the generator tool'
            finally:
                waiter.cancel()
                # Ending the terminal's client ends the leg.
                for term in terms.values():
                    await asyncio.to_thread(term.stop)
                await asyncio.wait_for(leg, timeout=180)
            await dead(host_url)
            old_url, old_host_url = url, host_url
        print('[D23-native] OpenCode1.18.30 actual tool dispatch and expired/replacement URL proved; publication uses SDK recording host')
    try:
        run_on_test_loop(run())
    finally:
        for term in terms.values():
            term.stop()
