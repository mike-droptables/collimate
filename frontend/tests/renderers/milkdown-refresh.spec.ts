import { displayFile, putTextFiles, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';

const raw = '# Milkdown refresh baseline\n\n- [ ] **Raw GFM task**\n\n' + Array.from({length:80},(_,i)=>`Paragraph ${i}.`).join('\n\n') + '\n';

test('actual Milkdown keeps its instance for identical raw bytes and external edits __F1841_F1818_F1842', async ({page,request},testInfo)=>{
  const channel = await createSeededChannel(request, `P3 Milkdown refresh ${Date.now()}`);

  // Edit the actual seeded package with the canonical SDK source; this exercises
  // the same unbuilt-package worker/upload flow as user renderer authoring.
  const registry=await (await request.get('/api/renderers?include_disabled=true')).json();
  const selected=registry.find((item:{id:string})=>item.id==='milkdown');
  const state=await (await request.get('/api/state')).json();
  const current=state.channels.find((item:{id:string})=>item.id===channel);
  const sourceCell=[...current.main_stacks,...current.nav_stacks].flatMap(stack=>stack.cells).find(cell=>cell.id===selected.cell_id);
  const definition=sourceCell.files.find((file:{name:string})=>file.name.endsWith('/milkdown.renderer'));
  const prefix=definition.name.slice(0,definition.name.lastIndexOf('/')+1);
  await putTextFiles(request, channel, sourceCell.id, [{
    name: prefix+'main.tsx',
    content: readFileSync(process.env.COLLIMATE_MILKDOWN_SOURCE_FILE??path.resolve('../content/renderers/documents/milkdown/main.tsx'),'utf8'),
    language: 'typescript',
  }]);
  const {cell_id}=await importCell(request,channel,'Milkdown refresh',[{name:'sample.md',content:raw}]);
  const saves:string[]=[];
  page.on('request',sent=>{
    if(sent.method()!=='POST'||!sent.url().includes(`/cells/${cell_id}/files/mutate`))return;
    const operation=sent.postDataJSON();
    if(operation?.mutations?.some((mutation:{path:string})=>mutation.path==='sample.md'))saves.push(sent.url());
  });
  await displayFile(request, channel, cell_id, 'sample.md', 'milkdown');
  await page.goto('/',{waitUntil:'domcontentloaded'});
  await expect(page.locator(`[data-cell-id="${cell_id}"]`)).toBeVisible();
  await page.locator(`[data-cell-id="${cell_id}"]`).getByRole('tab').filter({hasText:'sample.md'}).click();
  await expect.poll(async()=>{
    for(const candidate of page.frames())if(await candidate.locator('.ProseMirror').count())return candidate.url();
    return '';
  }).not.toBe('');
  const documentFrame=page.frames().find(candidate=>candidate.url().includes('renderer=milkdown'))!;
  const editor=documentFrame.locator('.ProseMirror');
  await expect(editor).toContainText('Raw GFM task');
  const scrollBefore=await editor.evaluate(node=>{
    (window as unknown as {milkdownBaseline:Element}).milkdownBaseline=node;
    let column=node.parentElement;
    while(column&&column.scrollHeight<=column.clientHeight)column=column.parentElement;
    if(!column)throw Error('expected actual scroll column');
    column.scrollTop=300;
    (window as unknown as {milkdownScroll:HTMLElement}).milkdownScroll=column;
    return column.scrollTop;
  });
  expect(scrollBefore).toBeGreaterThan(0);
  const host=await documentFrame.frameElement();
  const before=Date.now();
  await host.evaluate((node,cellId)=>{
    if(!(node instanceof HTMLIFrameElement))throw Error('missing actual iframe');
    node.contentWindow!.postMessage({type:'FILES_CHANGED',cellId,changedFiles:['sample.md']},location.origin);
  },cell_id);
  // Allow the actual asynchronous file read and content application to finish.
  await expect(editor).toContainText('Raw GFM task');
  await page.waitForTimeout(800);
  const sameRaw=await editor.evaluate(node=>node===(window as unknown as {milkdownBaseline:Element}).milkdownBaseline);
  await testInfo.attach('milkdown-before-profile',{contentType:'application/json',body:Buffer.from(JSON.stringify({sameRaw,elapsedMs:Date.now()-before,markup:await editor.innerHTML(),fixture:'canonical shipped Crepe7.21.2 GFM; original raw hyphen task list'}))});
  expect.soft(sameRaw,'an unchanged raw GFM file must retain the actual editor instance').toBe(true);
  await editor.evaluate(node=>{ (window as unknown as {milkdownBaseline:Element}).milkdownBaseline=node; });
  const changed=raw.replace('Raw GFM task','External GFM task');
  await putTextFiles(request, channel, cell_id, [{name:'sample.md',content:changed,language:'markdown'}]);
  await host.evaluate((node,cellId)=>{
    (node as HTMLIFrameElement).contentWindow!.postMessage({type:'FILES_CHANGED',cellId,changedFiles:['sample.md']},location.origin);
  },cell_id);
  await expect(editor).toContainText('External GFM task');
  expect(await editor.evaluate(node=>node===(window as unknown as {milkdownBaseline:Element}).milkdownBaseline),'external content uses the same actual editor DOM').toBe(true);
  expect(await documentFrame.evaluate('window.milkdownScroll.scrollTop')).toBe(scrollBefore);
  // Observe beyond the document owner's server-save debounce and external refresh.
  await page.waitForTimeout(2300);
  expect(saves,'external replacement and normalization must not echo a user save').toEqual([]);
});
