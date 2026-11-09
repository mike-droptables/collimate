"""Native extension mapping/cancellation/collision controls without runtime deps."""
import json
from pathlib import Path
import subprocess
import pytest
import sys

from tests._workspace import GENERATORS



def test_extension_preserves_schema_args_cancel_and_refuses_shadowed_tool_D23(tmp_path):
    source = GENERATORS / '_agent_shared/harnesses/pi/image/mcp.js'
    module = tmp_path / 'carrier.mjs'
    module.write_bytes(source.read_bytes())
    program = r'''
import {registerBridge} from MODULE;
const schema={type:'object',properties:{path:{type:'string'},settings:{type:'object'}}};
const definitions=[],handlers=new Map(),calls=[];
let owner=SOURCE,closed=false;
const pi={registerTool:tool=>definitions.push(tool),on:(name,fn)=>handlers.set(name,fn),
 getAllTools:()=>definitions.map(tool=>({name:tool.name,sourceInfo:{path:owner}}))};
const client={connect:async value=>{if(value!=='transport')throw Error('wrong transport')},
 listTools:async()=>({tools:[{name:'test_generator',description:'Actual generic command',inputSchema:schema}]}),
 callTool:async(args,_schema,options)=>{calls.push({args,signal:options.signal});return {content:[{type:'text',text:'host result'}]}},
 close:async()=>{closed=true}};
await registerBridge(pi,client,'transport');
if(definitions[0].parameters!==schema)throw Error('schema changed');
const input={path:'draft/probe.generator',settings:{nested:[1,null]}};
const controller=new AbortController();
const result=await definitions[0].execute('native-id',input,controller.signal);
if(calls[0].args.name!=='test_generator'||calls[0].args.arguments!==input||calls[0].signal!==controller.signal)throw Error('input/context changed');
if(await handlers.get('tool_call')({toolName:'collimate_test_generator'}))throw Error('owned tool blocked');
owner='/workspace/.pi/extensions/user.js';
const refused=await handlers.get('tool_call')({toolName:'collimate_test_generator'});
if(!refused.block||!refused.reason.includes('collision'))throw Error('shadow tool accepted');
if(handlers.get('tool_call')({toolName:'user_custom_tool'}))throw Error('user extension disabled');
await handlers.get('session_shutdown')();
if(!closed)throw Error('native transport leaked');
console.log(JSON.stringify({name:definitions[0].name,result,collisionBlocked:refused.block}));
'''.replace('MODULE', json.dumps(module.as_uri())).replace('SOURCE', json.dumps(str(module)))
    result = subprocess.run(['node', '--input-type=module', '-e', program],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof['name'] == 'collimate_test_generator'
    assert proof['result']['content'] == [{'type': 'text', 'text': 'host result'}]
    assert proof['collisionBlocked'] is True



_WRAPPER = GENERATORS / '_agent_shared/harnesses/pi/image/collimate-pi'

# A stand-in for pi-acp: spawns the wrapper directly and drops its stderr, as
# pi-acp's PiRpcProcess does.
_ADAPTER = ("const {spawn}=require('node:child_process');\n"
            "const child=spawn(process.argv[2],['--mode','rpc'],{stdio:'pipe',env:process.env});\n"
            "child.stdout.pipe(process.stdout);\n"
            "child.stderr.on('data', () => {});\n")


def _wrapper_with(tmp_path, pi_source: str) -> Path:
    """The real wrapper, with its fixed pi path pointed at a stand-in."""
    pi = tmp_path / 'pi'
    pi.write_text(f"#!{sys.executable}\n{pi_source}")
    pi.chmod(0o755)
    text = _WRAPPER.read_text()
    assert 'PI=/usr/bin/pi\n' in text
    wrapper = tmp_path / 'collimate-pi'
    wrapper.write_text(text.replace('PI=/usr/bin/pi\n', f'PI={pi}\n'))
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.parametrize('joined', [True, False])
def test_wrapper_joins_pi_stderr_to_the_adapters_only_on_the_acp_leg(tmp_path, joined):
    import os
    wrapper = _wrapper_with(tmp_path, "import sys; print('rpc-json'); sys.stderr.write('native collision reason\\n')\n")
    adapter = tmp_path / 'adapter.cjs'
    adapter.write_text(_ADAPTER)
    env = {k: v for k, v in os.environ.items() if k != 'COLLIMATE_PI_STDERR'}
    if joined:
        env['COLLIMATE_PI_STDERR'] = '1'
    delivered = subprocess.run(['node', str(adapter), str(wrapper)], capture_output=True, text=True,
                               timeout=10, env=env)
    assert delivered.returncode == 0, delivered.stderr
    assert delivered.stdout == 'rpc-json\n'
    assert delivered.stderr == ('native collision reason\n' if joined else '')


def test_joined_native_diagnostic_reaches_the_scrubbed_adapter_log_D23(tmp_path, native_bridge):
    import asyncio
    import os
    import shutil
    from _agent_shared import acp_client as ac, secret_hygiene as hygiene
    secret = 'pi-native-selected-synthetic-secret'
    wrapper = _wrapper_with(tmp_path, "import sys; print('{}'); sys.stderr.write('collision owners: root user ' + %r)\n" % secret)
    target = tmp_path / 'adapter.cjs'
    target.write_text(_ADAPTER)
    hygiene.clear()
    hygiene.remember(secret)
    async def run():
        adapter = await ac.spawn_host_adapter('pi-carrier', [shutil.which('node'), str(target), str(wrapper)],
                                              env={'COLLIMATE_PI_STDERR': '1', 'PATH': os.environ['PATH']},
                                              cwd=native_bridge.workspace)
        assert await adapter.transport.receive() == {}
        await adapter.proc.wait()
        await adapter.transport.close()
    try:
        asyncio.run(run())
        log = Path(native_bridge.workspace, '_agent/acp-pi-carrier.log').read_text()
        assert log == 'collision owners: root user [redacted]'
        assert secret not in log
    finally:
        hygiene.clear()


def test_native_intake_does_not_touch_planted_ready_marker_symlink_D23(tmp_path):
    """Execute exact factory filesystem path with isolated no-network client."""
    source = GENERATORS / '_agent_shared/harnesses/pi/image/mcp.js'
    module = tmp_path / 'mcp.mjs'
    config = tmp_path / 'collimate-mcp.json'
    config.write_text('{"url":"http://localhost/private/mcp"}')
    victim = tmp_path / 'unrelated.txt'
    victim.write_text('preserved unrelated bytes')
    (tmp_path / 'collimate-mcp-ready.json').symlink_to(victim)
    module.write_text(source.read_text().replace(
        "'/home/coder/.pi/agent/collimate-mcp.json'", json.dumps(str(config))))
    client_dir = tmp_path / 'node_modules/@modelcontextprotocol/sdk/dist/esm/client'
    client_dir.mkdir(parents=True)
    (tmp_path / 'node_modules/@modelcontextprotocol/sdk/package.json').write_text('{"type":"module"}')
    (client_dir / 'index.js').write_text('export class Client {async connect(){}; async listTools(){return {tools:[]}}; async close(){}}')
    (client_dir / 'streamableHttp.js').write_text('export class StreamableHTTPClientTransport {}')
    program = 'import factory from '+json.dumps(module.as_uri())+'; await factory({on(){},registerTool(){}});'
    result = subprocess.run(['node','--input-type=module','-e',program], capture_output=True,text=True,timeout=5)
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == 'preserved unrelated bytes'
    assert (tmp_path / 'collimate-mcp-ready.json').is_symlink()


def test_documented_reserved_names_cover_actual_pi_registry_union_P4R07(tmp_path):
    """Integrate the workspace registry, native prefix mapping and published list."""
    from itertools import product
    import re
    from types import SimpleNamespace
    from _agent_shared.generator_test import CommandContext
    from _agent_shared.workspace_tools import tool_entries

    names = set()
    context = CommandContext('host')
    skill = SimpleNamespace(name='synthetic', description='fixture', body='instructions')
    for files, web, skills, commands, placed, annotations in product(
        (False, True), (False, True), (None, [skill]), ('deny', 'ask', 'allow'),
        (None, context), (False, True),
    ):
        names.update(row[0] for row in tool_entries(
            enable_file_tools=files, websearch=web, skills=skills,
            commands=commands, command_context=placed, annotation_tools=annotations,
        ))
    root = Path(__file__).resolve().parents[1]
    module = tmp_path / 'registry.mjs'
    module.write_bytes((GENERATORS / '_agent_shared/harnesses/pi/image/mcp.js').read_bytes())
    tools = [{'name': name, 'description': 'fixture', 'inputSchema': {'type': 'object'}}
             for name in sorted(names)]
    program = r"""
import {registerBridge} from MODULE;
const registered=[];
const pi={registerTool:tool=>registered.push(tool.name),on(){}};
const client={connect:async()=>{},listTools:async()=>({tools:TOOLS})};
await registerBridge(pi,client,'synthetic');
console.log(JSON.stringify(registered));
""".replace('MODULE', json.dumps(module.as_uri())).replace('TOOLS', json.dumps(tools))
    result = subprocess.run(['node', '--input-type=module', '-e', program],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    reserved = json.loads(result.stdout)
    assert len(reserved) == len(names) == len(set(reserved))
    assert set(reserved) == {'collimate_' + name for name in names}
    document = (root / 'shared/docs/agent-runtime.md').read_text()
    documented = set(re.findall(r'`(collimate_[a-z_]+)`', document))
    assert documented == set(reserved), {
        'undocumented': sorted(set(reserved) - documented),
        'obsolete': sorted(documented - set(reserved)),
    }


@pytest.mark.parametrize('context', ['the session context\nsecond line', ''])
def test_wrapper_appends_the_acp_legs_session_context(tmp_path, context):
    """The ACP leg's context file becomes pi's --append-system-prompt; an empty
    or absent one appends nothing, and the variable never reaches pi."""
    import os
    wrapper = _wrapper_with(tmp_path, "import json, os, sys; print(json.dumps([sys.argv[1:], "
                                      "'COLLIMATE_PI_CONTEXT' in os.environ]))\n")
    seam = tmp_path / 'collimate-context.md'
    seam.write_text(context)
    env = {**os.environ, 'COLLIMATE_COMMANDS': 'allow', 'COLLIMATE_PI_CONTEXT': str(seam)}
    out = subprocess.run([str(wrapper), '--mode', 'rpc'], capture_output=True, text=True, timeout=10, env=env)
    assert out.returncode == 0, out.stderr
    argv, leaked = json.loads(out.stdout)
    assert leaked is False
    expected = ['--append-system-prompt', context] if context else []
    assert argv[argv.index('--approve'):] == ['--approve', '-e', '/opt/collimate/pi/mcp.js', *expected, '--mode', 'rpc']
