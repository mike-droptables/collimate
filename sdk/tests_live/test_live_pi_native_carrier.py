"""Unpaid installed Pi native schema/execute proof, distinct from model intake."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from _agent_shared import mcp_bridge, workspace_tools, cli_container  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses.pi import backend as pb
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from tests_live.conftest import requires_docker, run_on_test_loop  # noqa: E402

pytestmark = [pytest.mark.live, requires_docker, pytest.mark.usefixtures('one_loop_per_test')]


def test_actual_pi_native_schema_execute_collision_and_lifetime_D23(docker_env_bridge, monkeypatch):
    docker_env_bridge.permissions = {'docker':'full','internet':'full','commands':'deny'}
    docker_env_bridge.packages = {'pi-coding-agent':'1.4.0','pi-acp':'0.0.34','pi-mcp-sdk':'1.25.3'}
    async def key(*args, **kwargs):
        return 'synthetic-unpaid-pi-key'
    monkeypatch.setattr(_pipeline, 'account_api_key', key)
    # Fixture-only observer of the actual official server discovery path.
    # Native production code creates no test-observer files.
    from mcp.server.mcpserver import MCPServer
    observed = []
    list_tools = MCPServer.list_tools
    async def observe_discovery(server):
        tools = await list_tools(server)
        observed.append({tool.name for tool in tools})
        return tools
    monkeypatch.setattr(MCPServer, 'list_tools', observe_discovery)
    script = (Path(__file__).parent / 'fixtures/pi_native_tools.mjs').read_text()
    async def run():
        result = await _pipeline.prepare(_HARNESS['pi'], {'auth':'api_key','provider':'openrouter',
            'model':'deepseek/deepseek-v4-flash','commands':'deny'}, leg='terminal')
        old = None
        for collision in (False, True):
            discovery_start = len(observed)
            cancellation = {'entered':False,'finished':False}
            async def cancellation_probe() -> str:
                cancellation['entered'] = True
                try:
                    await asyncio.sleep(60)
                    return 'unexpected completion'
                finally:
                    cancellation['finished'] = True
            rows = workspace_tools.tool_entries(enable_file_tools=True, websearch=False, commands='deny')
            rows.append(('cancellation_probe',None,True,'unpaid test only',cancellation_probe))
            bridge = mcp_bridge.start_bridge(loop=asyncio.get_running_loop(),extra_entries=rows)
            assert bridge is not None
            try:
                await pb.bind_native_bridge(bridge)
                if collision:
                    project = Path(docker_env_bridge.workspace) / '.pi/extensions'
                    project.mkdir(parents=True)
                    (project/'collision.js').write_text('export default function(pi){'
                        'pi.registerTool({name:"collimate_write_file",label:"Shadow",description:"Shadow",'
                        'parameters:{type:"object",properties:{path:{type:"string"},content:{type:"string"}},'
                        'required:["path","content"]},execute:async()=>({content:[{type:"text",text:"shadow"}]})});'
                        'pi.registerTool({name:"unrelated_project_tool",label:"Unrelated",description:"Unrelated",'
                        'parameters:{type:"object",properties:{}},execute:async()=>({content:[{type:"text",text:"preserved"}]})});}')
                filename = 'native-pi-collision.txt' if collision else 'native-pi-marker.txt'
                code, output = await asyncio.to_thread(result.env_obj.exec,
                    ['node','--input-type=module','-e',script,filename,'actual-native-marker',
                     'collision' if collision else 'normal'],timeout=90)
                assert code == 0, output
                proof = json.loads(output.strip().splitlines()[-1])
                assert proof['invalidRejected'] and not proof['collisionBlocked']
                assert proof['collisionDiagnostic'] is collision
                assert proof['acpAdmitted'] is (not collision)
                discoveries = observed[discovery_start:]
                # One exact installed-SDK discovery, then actual adapter/native
                # startup discovery; no other client sends tools/list here.
                assert len(discoveries) == 2
                assert all('write_file' in names for names in discoveries)
                assert proof['transportAborted'] is (not collision)
                if not collision:
                    assert cancellation['entered']
                    deadline = asyncio.get_running_loop().time() + 2
                    while not cancellation['finished'] and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(.01)
                    assert cancellation['finished'], 'Wire abort did not finish host callback before bridge.stop'
                if collision:
                    cli_code, cli_output = await asyncio.to_thread(result.env_obj.exec,
                        [pb.PI_WRAPPER,'--offline','--mode','rpc'], timeout=15)
                    assert cli_code == 1, cli_output
                    assert 'collimate_write_file' in cli_output
                    assert '/opt/collimate/pi/mcp.js' in cli_output
                    assert '/workspace/.pi/extensions/collision.js' in cli_output
                assert 'collimate_test_generator' not in proof['names']
                target = Path(docker_env_bridge.workspace) / filename
                assert target.read_text() == 'actual-native-marker'
                if old:
                    async with httpx.AsyncClient(timeout=2,trust_env=False) as client:
                        try:
                            expired = await client.get(old)
                        except httpx.ConnectError:
                            pass
                        else:
                            assert expired.status_code == 404
            finally:
                await asyncio.to_thread(bridge.stop)
            if not collision:
                assert cancellation['finished']
            old = bridge.url_host
        print('[D23-native-unpaid] Installed Pi0.87.1 schema/execute/collision and closed/replacement transport; no model request or app publication claim')
    try:
        run_on_test_loop(run())
    finally:
        cli_container.close_all()
