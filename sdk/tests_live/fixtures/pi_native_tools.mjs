// Unpaid installed-native SDK measurement; no model request or account access.
import fs from 'node:fs/promises';
import {spawn} from 'node:child_process';
const root = '/usr/lib/node_modules/@earendil-works/pi-coding-agent';
const {createAgentSession} = await import(root + '/dist/core/sdk.js');
const {DefaultResourceLoader} = await import(root + '/dist/core/resource-loader.js');
const {SettingsManager} = await import(root + '/dist/core/settings-manager.js');
const {validateToolArguments} = await import(root + '/node_modules/@earendil-works/pi-ai/dist/utils/validation.js');
for (const filename of ['/opt/collimate/pi/mcp.js','/opt/collimate/pi/collimate-pi',
    '/opt/collimate/pi/patch-acp-stderr.cjs',await fs.realpath('/usr/bin/pi-acp')]) {
  const info=await fs.stat(filename);
  if(info.uid!==0 || (info.mode & 0o022)!==0) throw new Error('Native carrier is mutable');
  try {await fs.access(filename,2); throw new Error('Native carrier writable by runner');}
  catch(error){if(error.code!=='EACCES')throw error;}
}
const cwd = '/workspace', agentDir = '/home/coder/.pi/agent';
const [filename, content, mode] = process.argv.slice(1);
const settingsManager = SettingsManager.create(cwd, agentDir);
settingsManager.setProjectTrusted(true);
const loader = new DefaultResourceLoader({cwd, agentDir, settingsManager,
  additionalExtensionPaths: ['/opt/collimate/pi/mcp.js']});
await loader.reload();
const {session} = await createAgentSession({cwd, agentDir, resourceLoader: loader, settingsManager});
await session.bindExtensions({});
try {
  const tools = session._toolRegistry;
  const tool = tools.get('collimate_write_file');
  if (!tool) throw new Error('Native Pi did not receive collimate_write_file');
  let invalidRejected = false;
  try {validateToolArguments(tool, {name:tool.name, arguments:{path:{bad:true},content:'bad'}});}
  catch {invalidRejected = true;}
  if (!invalidRejected) throw new Error('Native schema accepted invalid path object');
  const args = validateToolArguments(tool, {name:tool.name,
    arguments:{path:filename,content}});
  const decision = await session._extensionRunner.emitToolCall({type:'tool_call',
    toolName:tool.name, toolCallId:'native-unpaid-proof', input:args});
  if (decision?.block) throw new Error(decision.reason);
  const output = await tool.execute('native-unpaid-proof',args);
  let transportAborted = false;
  if (mode === 'normal') {
    const cancellation = tools.get('collimate_cancellation_probe');
    if (!cancellation) throw new Error('Native cancellation probe missing');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error('native cancellation witness')), 200);
    try {
      await cancellation.execute('native-cancel', {}, controller.signal);
      throw new Error('Cancelled transport unexpectedly completed');
    } catch (error) {
      if (!controller.signal.aborted || !error.message.includes('native cancellation witness')) throw error;
      transportAborted = true;
    } finally {clearTimeout(timer);}
  }
  let collisionDiagnostic = false, acpAdmitted = false;
  if (mode === 'collision') {
    const definition = session.getAllTools().find(x => x.name === tool.name);
    if (definition?.sourceInfo?.path !== '/opt/collimate/pi/mcp.js')
      throw new Error('Native winning tool is not immutable carrier');
    const unrelated = tools.get('unrelated_project_tool');
    if (!unrelated || (await unrelated.execute('unrelated',{})).content[0].text !== 'preserved')
      throw new Error('Unrelated user extension was lost');
  }
  {
    const proc = spawn('pi-acp',[],{cwd,env:{...process.env,
      PI_OFFLINE:'1',COLLIMATE_COMMANDS:'deny',PI_ACP_PI_COMMAND:'/opt/collimate/pi/collimate-pi'},
      stdio:['pipe','pipe','pipe']});
    let stderr='', stdout='';
    const observed = new Promise((resolve,reject) => {
      const timer=setTimeout(()=>reject(new Error('Native collision diagnostic missing: '+JSON.stringify({stderr,stdout}))),15000);
      proc.stderr.on('data', chunk => {
        stderr += chunk.toString();
        if (stderr.includes('collimate_write_file') && stderr.includes('/opt/collimate/pi/mcp.js')
            && stderr.includes('/workspace/.pi/extensions/collision.js')) {
          clearTimeout(timer); resolve();
        }
      });
      proc.stdout.on('data', chunk => {
        stdout += chunk.toString();
        if (mode === 'normal') {
          for (const line of stdout.split('\n').filter(Boolean)) {
            let response;
            try {response=JSON.parse(line);} catch {continue;}
            if (response.id === 2 && typeof response.result?.sessionId === 'string') {
              clearTimeout(timer); acpAdmitted=true; resolve();
            }
            if (response.id === 2 && response.error) {
              clearTimeout(timer); reject(new Error(JSON.stringify(response.error)));
            }
          }
        }
      });
      proc.on('error',error=>{clearTimeout(timer);reject(error);});
    });
    try {
      proc.stdin.write(JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',
        params:{protocolVersion:1,clientCapabilities:{}}})+'\n');
      proc.stdin.write(JSON.stringify({jsonrpc:'2.0',id:2,method:'session/new',
        params:{cwd,mcpServers:[]}})+'\n');
      await observed;
      for (const line of stdout.trim().split('\n').filter(Boolean)) JSON.parse(line);
      collisionDiagnostic = mode === 'collision';
    } finally {
      proc.kill('SIGTERM');
      await new Promise(resolve=>{if(proc.exitCode!==null)resolve();else proc.once('exit',resolve);});
    }
  }
  console.log(JSON.stringify({names:[...tools.keys()], parameters:tool.parameters,
    invalidRejected,collisionBlocked:decision?.block===true,collisionDiagnostic,acpAdmitted,transportAborted,output}));
} finally {
  await session._extensionRunner.emit({type:'session_shutdown'});
  session.dispose();
}
