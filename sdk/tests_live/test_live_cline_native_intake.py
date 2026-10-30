"""Unpaid installed Cline core schema/call proof; native CLI intake remains separate."""
import asyncio
import json
from pathlib import Path

import pytest

from _agent_shared import cli_container, mcp_bridge, workspace_tools  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses.cline import backend as cb
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.acp_client import AcpSession, SpawnProfile, spawn_container_adapter  # noqa: E402
from tests_live.conftest import requires_docker, run_on_test_loop  # noqa: E402

pytestmark = [pytest.mark.live, requires_docker, pytest.mark.usefixtures('one_loop_per_test')]


def test_installed_cline_core_settings_schema_call_and_disposal_D23(docker_env_bridge, monkeypatch):
    docker_env_bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': 'deny'}
    docker_env_bridge.packages = {'cline': '3.0.73', 'mcp-remote': '0.15.2'}
    async def key(*args, **kwargs):
        return 'synthetic-unpaid-cline-key'
    monkeypatch.setattr(_pipeline, 'account_api_key', key)
    script = (Path(__file__).parent / 'fixtures/cline_native_tools.mjs').read_text()
    from mcp.server.mcpserver import MCPServer
    observed = []
    holding = asyncio.Event()
    entered = asyncio.Event()
    release = asyncio.Event()
    gate_owner = []
    released_discoveries = []
    list_tools = MCPServer.list_tools
    async def observe(server):
        tools = await list_tools(server)
        observed.append({tool.name for tool in tools})
        if holding.is_set():
            async def held_discovery():
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=30)
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                held_discovery(), gate_owner[0]))
            released_discoveries.append({tool.name for tool in tools})
        return tools
    monkeypatch.setattr(MCPServer, 'list_tools', observe)
    async def run():
        gate_owner.append(asyncio.get_running_loop())
        prepared = await _pipeline.prepare(_HARNESS['cline'], {'auth': 'api_key', 'provider': 'openrouter',
            'model': 'deepseek/deepseek-v4-flash', 'commands': 'deny'}, leg='acp')
        rows = workspace_tools.tool_entries(enable_file_tools=True, websearch=False, commands='deny')
        bridge = mcp_bridge.start_bridge(loop=asyncio.get_running_loop(), extra_entries=rows)
        assert bridge is not None
        try:
            await prepared.profile.bind_bridge(bridge)
            code, output = await asyncio.to_thread(prepared.env_obj.exec,
                ['node', '--input-type=module', '-e', script, cb.CLINE_MCP_POLICY_SETTINGS,
                 'native-cline-marker.txt', 'actual-unpaid-cline-native-marker'], timeout=90)
            assert code == 0, output
            proof = json.loads(output.strip().splitlines()[-1])
            assert proof['readBack'] and proof['settingsPreserved'] and proof['httpWriteRefused']
            assert (Path(docker_env_bridge.workspace) / 'native-cline-marker.txt').read_text() == 'actual-unpaid-cline-native-marker'
            core_discoveries = len(observed)
            session = AcpSession(backend='cline', profile=prepared.profile,
                mcp_servers_for=lambda caps: ([], []), model='deepseek/deepseek-v4-flash')
            prompt = None
            try:
                await asyncio.wait_for(session.start(), timeout=45)
                assert session.acp_session_id
                holding.set()
                from acp.schema import TextContentBlock
                prompt = asyncio.create_task(session.conn.prompt(session_id=session.acp_session_id,
                    prompt=[TextContentBlock(type='text', text='Unpaid native discovery cancellation witness')]))
                await asyncio.wait_for(entered.wait(), timeout=30)
                # Exact native code registers the abort controller before
                # ensureSessionManager. cancel synchronously sets its signal.
                # The following native mode response is an ordered ACP barrier
                # while tools/list remains held; runtime cannot reach send yet.
                await session.conn.cancel(session_id=session.acp_session_id)
                await asyncio.wait_for(session.conn.set_session_mode(
                    session_id=session.acp_session_id, mode_id='act'), timeout=5)
                assert not release.is_set() and not prompt.done()
                release.set()
                response = await asyncio.wait_for(prompt, timeout=20)
                assert response.stop_reason == 'cancelled'
                assert len(observed) > core_discoveries, 'Native ACP did not discover owned settings MCP'
                assert all('write_file' in names for names in observed[core_discoveries:])
                assert released_discoveries and all('write_file' in names for names in released_discoveries)
            finally:
                release.set()
                holding.clear()
                if prompt is not None and not prompt.done():
                    await session.conn.cancel(session_id=session.acp_session_id)
                    try:
                        await asyncio.wait_for(prompt, timeout=5)
                    except Exception:
                        pass
                await session.close()
            # Native OpenRouter providers.json is sufficient for TTY, but not
            # the pinned ACP authentication predicate. No prompt is submitted.
            terminal = await _pipeline.prepare(_HARNESS['cline'], {'auth': 'api_key', 'provider': 'openrouter',
                'model': 'deepseek/deepseek-v4-flash', 'commands': 'deny'}, leg='terminal')
            async def file_only_spawn():
                assert 'CLINE_API_KEY' not in terminal.env
                return await spawn_container_adapter('cline', terminal.env_obj,
                    ['cline', '--acp'], env_vars=terminal.env)
            file_only = AcpSession(backend='cline',
                profile=SpawnProfile(locus='container', cwd='/workspace', spawn=file_only_spawn,
                    commands='deny', env_obj=terminal.env_obj), mcp_servers_for=lambda caps: ([], []))
            try:
                with pytest.raises(Exception, match='Authentication required'):
                    await asyncio.wait_for(file_only.start(), timeout=30)
            finally:
                await file_only.close()
                await asyncio.to_thread(cb.remove_key_settings)
        finally:
            await asyncio.to_thread(bridge.stop)
        print('[D23-unpaid] Installed core schema/read/write + native ACP discovery/auth control; no prompt/model/publication claim')
    try:
        run_on_test_loop(run())
    finally:
        cli_container.close_all()


def test_actual_cline_terminal_idle_native_discovery_D23(docker_env_bridge, monkeypatch):
    """Actual runner teardown retires the old route before a fresh native leg."""
    from contextlib import asynccontextmanager
    import httpx
    import re
    import gen
    import _agent_shared
    docker_env_bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': 'deny'}
    docker_env_bridge.packages = {'cline': '3.0.73', 'mcp-remote': '0.15.2'}
    async def key(*args, **kwargs):
        return 'synthetic-unpaid-cline-key'
    monkeypatch.setattr(_pipeline, 'account_api_key', key)
    from mcp.server.mcpserver import MCPServer
    observed, owner, preparations, live, histories = [], [], [], [], []
    discovered = asyncio.Event()
    list_tools = MCPServer.list_tools
    async def observe(server):
        tools = await list_tools(server)
        observed.append({tool.name for tool in tools})
        owner[0].call_soon_threadsafe(discovered.set)
        return tools
    monkeypatch.setattr(MCPServer, 'list_tools', observe)
    prepare = cb.prepare
    async def capture_prepare(*args, **kwargs):
        prepared = await prepare(*args, **kwargs)
        preparations.append(prepared)
        return prepared
    monkeypatch.setattr(cb, 'prepare', capture_prepare)
    @asynccontextmanager
    async def terminal(container_id, command, **kwargs):
        prepared = preparations[-1]
        assert container_id == prepared.env_obj.container_id
        api = prepared.env_obj.container.client.api
        created = await asyncio.to_thread(api.exec_create, container_id, command,
            stdin=True, tty=True, workdir=kwargs['cwd'], environment=kwargs['env'])
        handle = await asyncio.to_thread(api.exec_start, created['Id'], tty=True, socket=True)
        live.append((api, created, handle))
        try:
            await asyncio.wait_for(discovered.wait(), timeout=30)
            yield {'terminal_id': 'unpaid-observed-native-cline'}
        finally:
            handle._response.close()
            handle.close()
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    @asynccontextmanager
    async def no_host_mirror(**kwargs):
        yield
    monkeypatch.setattr(gen, 'live_files', no_host_mirror)
    async def end_native_tty(*, terminal_id, on_action=None, poll=0.5):
        assert terminal_id == 'unpaid-observed-native-cline'
        api, created, handle = live[-1]
        handle._sock.sendall(b'\x03\x03')
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            status = await asyncio.to_thread(api.exec_inspect, created['Id'])
            if not status['Running']:
                break
            await asyncio.sleep(.05)
        assert not status['Running'], 'Native foreground did not exit'
        prepared = preparations[-1]
        import sqlite3
        home = Path(cb.key_settings_host_dir()).parent
        databases = list(home.rglob('sessions.db'))
        assert len(databases) == 1
        def stored_history():
            with sqlite3.connect(databases[0]) as database:
                return database.execute('SELECT session_id, conversation_id, prompt, messages_path FROM sessions ORDER BY session_id').fetchall()
        messages = home / 'unpaid-history-messages.json'
        message_bytes = b'[{"role":"user","content":"synthetic stored history witness"}]'
        messages.write_bytes(message_bytes)
        seed = '''const {SqliteSessionStore} = require('/usr/lib/node_modules/cline/node_modules/@cline/core/dist/index.js');
const store = new SqliteSessionStore({sessionsDir:process.argv[1]});
store.create({sessionId:'synthetic-history-witness',source:'cli',pid:0,startedAt:'2026-10-04T00:00:00Z',status:'completed',interactive:true,provider:'openrouter',model:'deepseek/deepseek-v4-flash',cwd:'/workspace',workspaceRoot:'/workspace',enableTools:true,enableSpawn:false,enableTeams:false,conversationId:'synthetic-conversation',prompt:'synthetic retained prompt',messagesPath:process.argv[2]}); store.close();'''
        db_dir = cb.CLINE_HOME + '/' + str(databases[0].parent.relative_to(home))
        rc, _ = await asyncio.to_thread(prepared.env_obj.exec,
            ['node', '-e', seed, db_dir, cb.CLINE_HOME + '/unpaid-history-messages.json'], timeout=10)
        assert rc == 0
        history = stored_history()
        assert history
        histories.append((databases[0], history, messages, message_bytes))
        return 'exited'
    monkeypatch.setattr(_agent_shared, 'serve_tty_until', end_native_tty)
    async def run():
        owner.append(asyncio.get_running_loop())
        old_url = None
        for leg in range(2):
            discovered.clear()
            start = len(observed)
            before_urls = cli_container.run_bridge_urls()
            await _agent_shared.surface_runner('cline', 'terminal')({'backend':'cline','auth':'api_key','provider':'openrouter',
                'model':'deepseek/deepseek-v4-flash','commands':'deny','websearch':False},
                pointer=gen.run_path('terminal/cline.terminal'))
            assert len(observed) > start and 'list_handlers' in observed[-1]
            assert 'test_generator' not in observed[-1]
            prepared = preparations[-1]
            database_path, history, messages, message_bytes = histories[-1]
            import sqlite3
            with sqlite3.connect(database_path) as database:
                preserved = database.execute('SELECT session_id, conversation_id, prompt, messages_path FROM sessions ORDER BY session_id').fetchall()
            assert preserved == history
            assert messages.read_bytes() == message_bytes
            rc, settings = await asyncio.to_thread(prepared.env_obj.exec,
                ['cat', cb.CLINE_MCP_POLICY_SETTINGS], timeout=5)
            assert rc == 0 and json.loads(settings) == {'mcpServers': {}}
            assert not (Path(cb.key_settings_host_dir()) / 'providers.json').exists()
            urls = cli_container.run_bridge_urls() - before_urls
            host_urls = [url for url in urls if not url.startswith('http://host.docker.internal:')]
            assert len(host_urls) == 1
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                for url in [host_urls[0]] + ([old_url] if old_url else []):
                    try:
                        response = await client.get(url)
                    except httpx.ConnectError:
                        pass
                    else:
                        assert response.status_code == 404
            old_url = host_urls[0]
            deadline = asyncio.get_running_loop().time() + 3
            while True:
                rc, processes = await asyncio.to_thread(prepared.env_obj.exec,
                    ['ps', '-eo', 'pid,ppid,stat,args'], timeout=5)
                assert rc == 0
                proxies = [row for row in processes.splitlines() if 'mcp-remote' in row or '--cline-hub-daemon' in row]
                if not proxies or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(.1)
            public_rows = [re.sub(r'https?://[^ ]+', '<private-endpoint>', row)
                for row in processes.splitlines() if 'mcp-remote' in row or '.cline' in row]
            print('[Cline-native-owned-processes-after-actual-teardown]', public_rows)
            assert not proxies, 'Native proxy survived actual runner teardown'
    try:
        run_on_test_loop(run())
    finally:
        cli_container.close_all()
