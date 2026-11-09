"""Always-on handler tools reach the actual MCP bridge."""
import asyncio
from types import SimpleNamespace


import _agent_shared as eng
from _agent_shared import workspace_tools  # noqa: E402

NAMES = ['list_handlers', 'handler_skills', 'validate_file']


def test_handler_tools_are_present_with_both_workspace_axes_off__D18():
    rows = workspace_tools.tool_entries(enable_file_tools=False, websearch=False)
    assert [row[0] for row in rows] == NAMES


def test_empty_gateway_and_terminal_legs_still_start_owned_bridges__D18(monkeypatch):
    import _agent_shared.cli_container as cc
    import _agent_shared.mcp_bridge as bridge_module
    calls, stopped = [], []
    def start(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(url_container='http://container/owned',url_host='http://host/owned',stop=lambda:stopped.append(True))
    monkeypatch.setattr(bridge_module,'start_bridge',start)
    monkeypatch.setattr(cc,'_BRIDGES',{})
    monkeypatch.setattr(cc,'_LEG_BRIDGE_URLS',set())
    async def run():
        gateway=cc.gateway_bridge(bind_host='127.0.0.1')
        assert gateway is not None
        assert cc.gateway_bridge(bind_host='127.0.0.1') is gateway
        async with cc.leg_bridge() as terminal:
            assert terminal is not None
        assert len(calls)==2 and stopped==[True]
    asyncio.run(run())


def test_actual_local_mcp_lists_and_dispatches_handler_tools__D18(monkeypatch, native_bridge):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import _agent_shared.mcp_bridge as bridge_module
    calls=[]
    eng.gen.update_file('pkg/input.note', 'title')
    eng.gen.update_file('pkg/verify.ts', 'export default () => true')
    monkeypatch.setattr(eng.gen,'handlers',lambda: [{'id':'note','types':[{'ext':'.note'}]}])
    monkeypatch.setattr(eng.gen,'handler_skills',lambda value:calls.append(('skills',value)) or {'note--write/SKILL.md':'Write a title'})
    monkeypatch.setattr(eng.gen,'validate',lambda path,verifier=None:calls.append(('validate',path,verifier)) or {'valid':False,'error':'Title required'})
    async def run():
        bridge=bridge_module.start_bridge(loop=asyncio.get_running_loop(),bind_host='127.0.0.1')
        assert bridge is not None
        try:
            for _ in range(200):
                if bridge._server.started:break
                await asyncio.sleep(.01)
            assert bridge._server.started
            import httpx
            async with httpx.AsyncClient() as client:
                refused=await client.post(f'http://127.0.0.1:{bridge.port}/mcp',json={})
                assert refused.status_code==404
                bad_host=await client.post(bridge.url_host,json={},headers={'Host':'foreign.example','Accept':'application/json, text/event-stream'})
                assert bad_host.status_code==421
            async with streamable_http_client(bridge.url_host) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    listed=await session.list_tools()
                    assert [tool.name for tool in listed.tools]==NAMES
                    for name,args in [('list_handlers',{}),('handler_skills',{'ext_or_id':'.note'}),('validate_file',{'path':'pkg/input.note','verifier':'pkg/verify.ts'})]:
                        result=await session.call_tool(name,args)
                        assert not result.is_error,result
                        assert result.content
                        expected={'list_handlers':'note','handler_skills':'Write a title','validate_file':'Title required'}[name]
                        assert expected in str(result.content)
            assert calls==[('skills','.note'),('validate','pkg/input.note','pkg/verify.ts')]
        finally:
            await asyncio.to_thread(bridge.stop)
        assert not bridge._thread.is_alive()
    asyncio.run(run())


def test_handler_tool_identity_cannot_be_overridden_and_is_not_duplicated__D18():
    import pytest
    rows=workspace_tools.tool_entries(enable_file_tools=False,websearch=False)
    assert [row[0] for row in workspace_tools.with_handler_tools(rows)] == NAMES
    with pytest.raises(ValueError,match='cannot be replaced'):
        workspace_tools.with_handler_tools([('validate_file','handler',False,'',lambda:None)])
