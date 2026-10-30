import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {test,expect,type Page,type APIRequestContext} from '@playwright/test';
import {installCandidate} from './installCandidate';

type EditRecord={editSeq:number;chars:number;tail:string};

async function openFile(page:Page,request:APIRequestContext,id:string,content:string){
 const channel = await createSeededChannel(request, `P3 ${id} behavior ${Date.now()}`);

 await installCandidate(request,channel,id);
 const filename=id==='excalidraw'?'scene.excalidraw':'source.txt';
 const {cell_id}=await importCell(request,channel,`P3 ${id} behavior`,[{name:filename,content}]);
 await displayFile(request, channel, cell_id, filename, id);
 const networkWrites:EditRecord[]=[];
 page.on('request',sent=>{
  if(sent.method()!=='POST'||!sent.url().includes(`/cells/${cell_id}/files/mutate`))return;
  const operation=sent.postDataJSON();
  for(const mutation of operation?.mutations??[]){
   if(mutation.path===filename&&typeof mutation.content_text==='string')networkWrites.push({editSeq:mutation.edit_seq,chars:mutation.content_text.length,tail:mutation.content_text.slice(-100)});
  }
 });
 await page.addInitScript(({cell_id,filename})=>{
  if(window===window.top){
   (window as any).__editorJournalWrites=[];
   const put=IDBObjectStore.prototype.put;
   IDBObjectStore.prototype.put=function(value:any,key?:IDBValidKey){
    const record=value?.record;
    if(this.name==='documents'&&record?.identity?.cellId===cell_id&&record.identity.path===filename){
     (window as any).__editorJournalWrites.push({editSeq:record.editSeq,chars:record.body.length,tail:record.body.slice(-100)});
    }
    return key===undefined?put.call(this,value):put.call(this,value,key);
   };
  }
 },{cell_id,filename});
 await page.goto('/',{waitUntil:'domcontentloaded'});
 return {channel,cell_id,filename,networkWrites};
}

test('opening a drawing does not normalize it into a user edit __F1803',async({page,request})=>{
 const original=JSON.stringify({type:'excalidraw',version:2,source:'external-tool',elements:[],appState:{theme:'light',viewBackgroundColor:'#ffffff'},files:{}});
 const {cell_id,channel,filename,networkWrites}=await openFile(page,request,'excalidraw',original);
 await expect.poll(async()=>{for(const frame of page.frames())if(await frame.locator('.excalidraw').count())return true;return false;},{timeout:90000}).toBe(true);
 await page.waitForTimeout(1500);
 const writes=await page.evaluate(()=>(window as any).__editorJournalWrites);
 await test.info().attach('opening-writes.json',{body:JSON.stringify(writes,null,2),contentType:'application/json'});
 expect(writes).toHaveLength(0);
 expect(networkWrites).toHaveLength(0);
 const response=await request.get(`/api/channels/${encodeURIComponent(channel)}/cells/${cell_id}/files/${filename}`);
 expect(response.ok()).toBe(true);expect(await response.text()).toBe(original);
 let drawingFrame=page.mainFrame();for(const frame of page.frames())if(await frame.locator('.excalidraw canvas').count()){drawingFrame=frame;break;}
 // Exercise the real host's theme broadcast without changing persisted data.
 for(const scheme of ['light','dark']){
  await page.evaluate(scheme=>window.dispatchEvent(new CustomEvent('collimate:theme-changed',{detail:{scheme,vars:{}}})),scheme);
  await page.waitForTimeout(650);
  expect(await page.evaluate(()=>(window as any).__editorJournalWrites)).toHaveLength(0);
  expect(networkWrites).toHaveLength(0);
 }
 await page.locator(`[data-cell-id="${cell_id}"]`).getByRole('tab').filter({hasText:filename}).click();
 const canvas=drawingFrame.locator('.excalidraw canvas').first();const box=await canvas.boundingBox();expect(box).not.toBeNull();
 await drawingFrame.getByRole('radio',{name:'Rectangle',exact:true}).locator('..').click();
 await page.mouse.move(box!.x+180,box!.y+160);await page.mouse.down();await page.mouse.move(box!.x+300,box!.y+260,{steps:8});await page.mouse.up();
 await expect.poll(async()=>JSON.parse(await (await request.get(`/api/channels/${encodeURIComponent(channel)}/cells/${cell_id}/files/${filename}`)).text()).elements.length).toBeGreaterThan(0);
 const edited=await (await request.get(`/api/channels/${encodeURIComponent(channel)}/cells/${cell_id}/files/${filename}`)).json();expect(edited.appState.theme).toBe('light');
});

test('large Monaco typing coalesces full-file saves and flushes the last edit __F1839',async({page,request})=>{
 const {cell_id,channel,filename,networkWrites}=await openFile(page,request,'monaco',('plain text line '+'.'.repeat(190)+'\n').repeat(2500));
 await expect.poll(async()=>{for(const frame of page.frames())if(await frame.locator('.monaco-editor textarea').count())return true;return false;},{timeout:90000}).toBe(true);
 let editorFrame=page.mainFrame();for(const candidate of page.frames())if(await candidate.locator('.monaco-editor textarea').count()){editorFrame=candidate;break;}
 const overlay=page.locator('[title="Click to focus"]');if(await overlay.count())await overlay.first().click();
 const field=editorFrame.locator('.monaco-editor textarea').first();await field.focus();await field.press('Control+End');
 await page.evaluate(()=>(window as any).__editorJournalWrites=[]);networkWrites.length=0;
 const start=performance.now();await field.pressSequentially('0123456789abcde',{delay:20});const typingMs=performance.now()-start;
 await page.waitForTimeout(650);
 const writes:EditRecord[]=await page.evaluate(()=>(window as any).__editorJournalWrites);
 await test.info().attach('large-file-typing.json',{body:JSON.stringify({typingMs,writes,networkWrites},null,2),contentType:'application/json'});
 expect(writes.length).toBeGreaterThan(0);expect(writes.at(-1)?.tail).toContain('0123456789abcde');
 expect(networkWrites.length).toBeGreaterThan(0);expect(networkWrites.length).toBeLessThan(5);
 await field.pressSequentially('LAST100MS',{delay:1});await page.waitForTimeout(100);
 // Close shows on tab hover, as for a user.
 const close=page.locator(`[data-cell-id="${cell_id}"]`).getByRole('button',{name:`Close ${filename}`,exact:true});await close.hover({force:true});await close.click();
 await expect.poll(async()=>await (await request.get(`/api/channels/${encodeURIComponent(channel)}/cells/${cell_id}/files/${filename}`)).text()).toContain('0123456789abcdeLAST100MS');
});

test('small Monaco files retain immediate local snapshots __F1839',async({page,request})=>{
 const {cell_id,channel,filename,networkWrites}=await openFile(page,request,'monaco','small file');
 await expect.poll(async()=>page.frames().some(frame=>frame.url().includes('renderer=monaco'))).toBe(true);
 let editorFrame=page.mainFrame();
 await expect.poll(async()=>{for(const frame of page.frames())if(await frame.locator('.monaco-editor textarea').count()){editorFrame=frame;return true;}return false;}).toBe(true);
 const field=editorFrame.locator('.monaco-editor textarea').first();await field.focus();await field.press('Control+End');
 await page.evaluate(()=>(window as any).__editorJournalWrites=[]);await field.pressSequentially('0123456789',{delay:20});
 await expect.poll(async()=>await page.evaluate(()=>(window as any).__editorJournalWrites.at(-1)?.editSeq??0)).toBeGreaterThanOrEqual(10);
 const writes:EditRecord[]=await page.evaluate(()=>(window as any).__editorJournalWrites);
 expect(writes.at(-1)?.tail).toContain('0123456789');
 await expect.poll(()=>networkWrites.at(-1)?.editSeq??0).toBeGreaterThanOrEqual(10);
 expect(await(await request.get(`/api/channels/${channel}/cells/${cell_id}/files/${filename}`)).text()).toContain('0123456789');
});
