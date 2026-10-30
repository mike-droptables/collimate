import { expect, type APIRequestContext } from '@playwright/test';
import { randomUUID } from 'node:crypto';

/** Seed a real durable display descriptor before loading a fresh app page. */
export async function displayFile(request: APIRequestContext, channel: string, cellId: string,
  path: string, rendererId?: string): Promise<void> {
  const leaf = randomUUID(), tab = randomUUID();
  const response = await request.put(`/api/channels/${channel}/cell_display/${cellId}`, { data: {
    version: 1, root: { type: 'leaf', id: leaf, tabs: [
      { id: tab, kind: 'file', path, ...(rendererId === undefined ? {} : { rendererId }) },
    ], activeTabId: tab }, activeLeafId: leaf,
    filesPane: { collapsed: false, side: 'left', width: null, height: null, viewMode: 'tree', panelFilters: { runsOnly: false, sortByRecency: false } },
    runsPane: { open: false, side: 'right', width: null, height: null },
    pinnedTabCompact: false, history: null,
  } });
  expect(response.ok(), await response.text()).toBe(true);
}

/** Conditional put with exact receipt checks; unchanged siblings stay untouched. */
export async function putTextFiles(request: APIRequestContext, channel: string, cellId: string,
  files: { name: string; content: string; language?: string }[]): Promise<void> {
  const inventory = await request.get(`/api/channels/${channel}/cells/${cellId}/files`);
  expect(inventory.ok(), await inventory.text()).toBe(true);
  const descriptors = (await inventory.json()).files as { path: string; file_revision: number }[];
  const operationId = randomUUID(), writer = `renderer-fixture:${randomUUID()}`;
  const mutations = files.map(file => ({ kind: 'put', path: file.name, content_text: file.content,
    ...(file.language === undefined ? {} : { language: file.language }), writer_id: writer,
    edit_seq: 1, base_revision: descriptors.find(row => row.path === file.name)?.file_revision ?? 0 }));
  const response = await request.post(`/api/channels/${channel}/cells/${cellId}/files/mutate`, {
    data: { operation_id: operationId, mutations, resolves: [] },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const receipt = await response.json();
  expect(receipt.operation_id).toBe(operationId);
  expect(receipt.outcome).toBe('accepted');
  expect(receipt.files.map((row: { path: string }) => row.path).sort()).toEqual(files.map(row => row.name).sort());
  for (const mutation of mutations) {
    expect(receipt.files.find((row: { path: string }) => row.path === mutation.path)).toMatchObject({
      writer_id: writer, edit_seq: 1, deleted: false,
    });
    expect(receipt.files.find((row: { path: string }) => row.path === mutation.path).file_revision).toBeGreaterThan(mutation.base_revision);
  }
}

/** A new cell from inline bodies through the import route: each body streams
 *  into the CAS (POST /api/blob/stream), then one unnamed stack of one cell is
 *  committed (POST /import). A file that already carries a blob_hash is
 *  referenced as is. */
export async function importCell(request: APIRequestContext, channel: string, name: string,
  files: { name: string; content?: string; blob_hash?: string }[]): Promise<{ cell_id: string; stack_id: string }> {
  const stored: { path: string; blob_hash: string }[] = [];
  for (const file of files) {
    let hash = file.blob_hash;
    if (hash === undefined) {
      const upload = await request.post('/api/blob/stream', {
        data: Buffer.from(file.content ?? '', 'utf8'),
        headers: { 'Content-Type': 'application/octet-stream' },
      });
      expect(upload.ok(), await upload.text()).toBe(true);
      hash = (await upload.json()).blob_hash as string;
    }
    stored.push({ path: file.name, blob_hash: hash });
  }
  const response = await request.post(`/api/channels/${encodeURIComponent(channel)}/import`, {
    data: { insert_index: null, stacks: [{ name: '', cells: [{ name, files: stored, dirs: [] }] }] },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const { stack_ids, cell_ids } = await response.json();
  return { cell_id: cell_ids[0], stack_id: stack_ids[0] };
}
