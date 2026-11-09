// Installed Cline core intake, distinct from native CLI/model delivery.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  InMemoryMcpManager, createDefaultMcpServerClientFactory,
  registerMcpServersFromSettingsFile, createMcpTools,
} from '/usr/lib/node_modules/cline/node_modules/@cline/core/dist/index.js';

const [settingsPath, filename, marker] = process.argv.slice(1);
const before = fs.readFileSync(settingsPath);
const manager = new InMemoryMcpManager({clientFactory:
  createDefaultMcpServerClientFactory({settingsPath})});
try {
  const servers = await registerMcpServersFromSettingsFile(manager, {filePath: settingsPath});
  assert.equal(servers.length, 1);
  assert.equal(servers[0].name, 'collimate');
  assert.equal(servers[0].transport.type, 'stdio');
  assert.equal(servers[0].timeoutSeconds, 1800);
  const tools = await createMcpTools({serverName: 'collimate', provider: manager});
  const names = tools.map(tool => tool.name);
  assert(names.includes('collimate__write_file'));
  assert(names.includes('collimate__read_file'));
  assert(!names.includes('collimate__test_generator'));
  assert(!names.includes('collimate__run_command'));
  const write = tools.find(tool => tool.name === 'collimate__write_file');
  assert.deepEqual(write.inputSchema.required, ['path', 'content']);
  const context = {abortSignal: new AbortController().signal};
  const written = await write.execute({path: filename, content: marker}, context);
  const read = await tools.find(tool => tool.name === 'collimate__read_file')
    .execute({path: filename}, context);
  assert(JSON.stringify(read).includes(marker));
  assert(!written?.isError);
  assert(fs.readFileSync(settingsPath).equals(before), 'Stdio intake rewrote protected settings');
  const http = new InMemoryMcpManager({clientFactory:
    createDefaultMcpServerClientFactory({settingsPath})});
  let httpWriteRefused = false;
  try {
    await http.registerServer({name: 'collimate', transport: {
      type: 'streamableHttp', url: servers[0].transport.args[0]}});
    try {
      await http.connectServer('collimate');
    } catch (error) {
      assert.match(String(error), /EACCES|permission denied|read-only/i);
      httpWriteRefused = true;
    }
    assert(httpWriteRefused, 'HTTP OAuth path unexpectedly rewrote protected settings');
    assert(fs.readFileSync(settingsPath).equals(before));
  } finally {
    await http.dispose();
  }
  console.log(JSON.stringify({names, readBack: true, settingsPreserved: true, httpWriteRefused}));
} finally {
  await manager.dispose();
}
