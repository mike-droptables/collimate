import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {expect,test,type APIRequestContext} from '@playwright/test';

type File = {name:string;content:string};
async function post(request:APIRequestContext,url:string,data:unknown){
 const response=await request.post(url,{data});expect(response.ok(),await response.text()).toBe(true);return response.json();
}
/** Add the package at `files[0]` of a new cell holding `files` to Capabilities, in a new cell. */
async function approve(request:APIRequestContext,channel:string,files:File[]){
 const base=`/api/channels/${encodeURIComponent(channel)}`;
 const cell=await importCell(request,channel,'Flush fixture source',files);
 const args={source_cell_id:cell.cell_id,definition_path:files[0].name,target_cell_id:null};
 const preview=await post(request,base+'/packages/preview',args);
 await post(request,base+'/packages/add',{...args,expected_content_hash:preview.content_hash,expected_target_revision:preview.target_revision,replace:false});
}

for(const embed of ['native','iframe']){
 test(`edit typed100ms before Run reaches the actual subprocess in ${embed} host __F0691_F0684 (P02)`,async({page,request})=>{
  test.setTimeout(180000);
  const channel = await createSeededChannel(request, `Flush ${embed} ${Date.now()}`);
  const source=`import React,{useState,useEffect} from 'https://esm.sh/react@19.3.1?target=es2022';
import {defineRenderer,useCollimateFile} from '/api/sdk/react.js';
function Editor(){const {content,saveContent}=useCollimateFile();const [text,setText]=useState(content);useEffect(()=>setText(content),[content]);return React.createElement('textarea',{'aria-label':'Run flush editor',value:text,onChange:e=>{setText(e.target.value);saveContent(e.target.value);}});}export default defineRenderer({component:Editor});`;
  await approve(request,channel,[{name:'editor/editor.renderer',content:`id: flush-${embed}\nname: Flush editor\nsummary: Immediate document save before Run\nentrypoint: main.tsx\nembed: ${embed}\nextensions: [.flushproof]\n`},{name:'editor/main.tsx',content:source}]);
  await approve(request,channel,[{name:'observer/observer.generator',content:'id: flush-observer\nname: Flush observer\nsummary: Records actual run input\nentrypoint: run.py\nmain_config: settings.set_yaml\ndraft_files: [settings.set_yaml]\naccount_requirements: {}\n'},{name:'observer/settings.set_yaml',content:'fields: []\n'},{name:'observer/run.py',content:"from collimate import native as gen\nasync def run():\n    gen.update_file('observed.txt', gen.read_file('input.flushproof'))\n    await gen.commit()\nif __name__ == '__main__':\n    gen.main(run)\n"}]);
  await post(request,'/api/runners',{runner:{id:'flush-observer',generator:'flush-observer',name:'Flush observer',description:'Records exact input',prompt:'Read input',settings:{'settings.set_yaml':{}},accounts:{},folders:[],permissions:{docker:'none',internet:'full',commands:'deny',update_packages:false},packages:{}},files:[]});
  const base=`/api/channels/${encodeURIComponent(channel)}`;
  const work=await importCell(request,channel,'Flush work',[{name:'input.flushproof',content:'original'}]);
  await displayFile(request, channel, work.cell_id, 'input.flushproof', `flush-${embed}`);
  await page.goto('/');const cell=page.locator(`[data-cell-id="${work.cell_id}"]`);
  let editor=cell.getByRole('textbox',{name:'Run flush editor'});
  if(embed==='iframe'){
   await expect.poll(async()=>{for(const frame of page.frames()){const candidate=frame.getByRole('textbox',{name:'Run flush editor'});if(await candidate.count()){editor=candidate;return true;}}return false;},{timeout:90000}).toBe(true);
  }
  await expect(editor).toHaveValue('original',{timeout:90000});
  const overlay=cell.locator('[title="Click to focus"]');if(await overlay.count())await overlay.click();
  await editor.fill(`latest ${embed} edit`);
  await page.waitForTimeout(100);
  // The Run shortcut, from inside the editor, opens the cell's runners panel.
  await page.keyboard.press('Alt+r');
  await cell.getByRole('option',{name:/Flush observer/}).dblclick();
  await expect.poll(async()=>{const response=await request.get(`${base}/cells/${work.cell_id}/files/observed.txt`);return response.ok()?response.text():'';},{timeout:60000}).toBe(`latest ${embed} edit`);
  expect(await(await request.get(`${base}/cells/${work.cell_id}/files/input.flushproof`)).text()).toBe(`latest ${embed} edit`);
 });
}
