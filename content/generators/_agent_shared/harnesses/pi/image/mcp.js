// Root-owned native intake for this run's common Collimate MCP bridge.
import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const CONFIG = '/home/coder/.pi/agent/collimate-mcp.json';
const SOURCE = fileURLToPath(import.meta.url);
const PREFIX = 'collimate_';

export async function registerBridge(pi, client, transport) {
  await client.connect(transport);
  const names = new Set();
  let cursor;
  do {
    const page = await client.listTools(cursor ? {cursor} : undefined, {timeout: 10000});
    for (const tool of page.tools) {
      const name = PREFIX + tool.name;
      if (names.has(name)) throw new Error(`Duplicate Collimate tool: ${name}`);
      names.add(name);
      pi.registerTool({
        name, label: tool.title || tool.name, description: tool.description || tool.name,
        parameters: tool.inputSchema,
        async execute(_id, args, signal) {
          const result = await client.callTool({name: tool.name, arguments: args}, undefined,
            {signal, timeout: 1800000});
          if (result.isError) throw new Error(result.content.filter(x => x.type === 'text')
            .map(x => x.text).join('\n') || 'Collimate tool refused');
          return {content: result.content, details: result.structuredContent || {}};
        },
      });
    }
    cursor = page.nextCursor;
  } while (cursor);
  // Native CLI startup refuses conflicting extension names with diagnostics.
  // This extra guard also refuses a foreign winning owner in SDK execution;
  // it does not infer that the public tool list exposes overridden losers.
  pi.on('tool_call', event => {
    if (!names.has(event.toolName)) return;
    const installed = pi.getAllTools().find(tool => tool.name === event.toolName);
    if (installed?.sourceInfo?.path !== SOURCE)
      return {block: true, reason: `Collimate tool name collision: ${event.toolName}`};
  });
  pi.on('session_shutdown', async () => { await client.close(); });
  return names;
}

export default async function (pi) {
  let value;
  try { value = JSON.parse(await fs.readFile(CONFIG, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return; throw error; }
  if (!value || typeof value.url !== 'string' || Object.keys(value).join() !== 'url')
    throw new Error('Invalid Collimate native bridge binding');
  const url = new URL(value.url);
  if (url.protocol !== 'http:' && url.protocol !== 'https:')
    throw new Error('Invalid Collimate native bridge URL');
  const {Client} = await import('./node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js');
  const {StreamableHTTPClientTransport} = await import('./node_modules/@modelcontextprotocol/sdk/dist/esm/client/streamableHttp.js');
  const client = new Client({name: 'collimate-pi', version: '1'}, {capabilities: {}});
  try {
    await registerBridge(pi, client, new StreamableHTTPClientTransport(url));
  } catch (error) {
    await client.close();
    throw error;
  }
}
