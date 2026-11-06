import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {expect,test,type APIRequestContext} from '@playwright/test';

type File={name:string,content:string};
async function checked(request:APIRequestContext,url:string,data:unknown){const r=await request.post(url,{data});expect(r.ok(),await r.text()).toBe(true);return r.json();}
async function createCell(request:APIRequestContext,channel:string,files:File[],name='Synthetic approval work'){return importCell(request,channel,name,files);}
async function capabilitiesStack(request:APIRequestContext,channel:string){
 const state=await (await request.get('/api/state')).json();const ch=state.channels.find((c:any)=>c.id===channel);
 const stack=[...ch.nav_stacks,...ch.main_stacks].find((s:any)=>s.stack_type==='capabilities');expect(stack).toBeTruthy();return stack;
}
async function previewAdd(request:APIRequestContext,channel:string,cell:string,definition:string,target?:string){
 const args={source_cell_id:cell,definition_path:definition,target_cell_id:target??null};
 return {args,preview:await checked(request,`/api/channels/${encodeURIComponent(channel)}/packages/preview`,args)};
}
/** Add the package at `definition` of `cell` to Capabilities: into a new
 *  cell, or replacing it in the capability cell `target`. */
async function approve(request:APIRequestContext,channel:string,cell:string,definition:string,target?:string){
 const {args,preview}=await previewAdd(request,channel,cell,definition,target);
 const added=await checked(request,`/api/channels/${encodeURIComponent(channel)}/packages/add`,{...args,expected_content_hash:preview.content_hash,expected_target_revision:preview.target_revision,replace:!!target});
 expect((await capabilitiesStack(request,channel)).cells.some((c:any)=>c.id===added.cell_id)).toBe(true);
 return added;
}
async function raw(request:APIRequestContext,channel:string,cell:string,file:string){return request.get(`/api/channels/${encodeURIComponent(channel)}/cells/${cell}/files/${file}`);}
async function run(request:APIRequestContext,channel:string,cell:string){
 const started=await checked(request,`/api/channels/${encodeURIComponent(channel)}/runs`,{runner:'integration-proof',cell_id:cell});
 const id=started.run_id;
 await expect.poll(async()=>{const rows=await(await request.get(`/api/channels/${encodeURIComponent(channel)}/runs`)).json();return rows.find((r:any)=>r.id===id)?.status;},{timeout:60000}).toMatch(/completed|failed|stopped/);
 const rows=await(await request.get(`/api/channels/${encodeURIComponent(channel)}/runs`)).json();expect(rows.find((r:any)=>r.id===id),JSON.stringify(rows)).toMatchObject({status:'completed'});return id;
}
const rendererSource=`import React,{useEffect,useState} from 'https://esm.sh/react@19.3.1?target=es2022';
import {defineRenderer,useCollimateFile} from '/api/sdk/react.js';
function Editor(){const {api}=useCollimateFile();const [result,setResult]=useState('ready');
useEffect(()=>api.onFlush(()=>api.saveSiblingFile('exit.txt','saved on actual exit').then(ok=>{if(!ok&&!api.isReadOnly())throw Error('exit refused');})),[api]);
return React.createElement('div',null,React.createElement('div',null,'Real generated renderer'),...['sample.proof','pkg/main.tsx','blocked.renderer','blocked.generator','blocked.runner','blocked.handler','blocked.options_stack'].map(p=>React.createElement('button',{key:p,onClick:async()=>setResult(p+':'+await api.saveSiblingFile(p,'written '+p))},'Write '+p)),React.createElement('output',null,result));}
export default defineRenderer({component:Editor});`;
const rendererManifest='id: integration-renderer\nname: Integration renderer\nsummary: Deterministic actual run output\nentrypoint: main.tsx\nembed: native\nextensions: [.proof]\n';
const outputFiles={'pkg/view.renderer':rendererManifest,'pkg/main.tsx':rendererSource,'sample.proof':'original sample','preview.renderer_example':JSON.stringify({renderer:'pkg/view.renderer',file:'sample.proof'})};
const verifier='export default function(input){return input.text === "accepted value" ? {valid:true} : {valid:false,error:"integration exact value required"};}';
async function installGenerator(request:APIRequestContext,channel:string){
 const script=`from collimate import native as gen\nasync def run():\n    if 'brief.md' in gen.list_files():\n        gen.update_file('custom/derived/verify.ts', ${JSON.stringify(verifier)})\n        await gen.commit()\n    else:\n        import json\n        for name,content in json.loads(${JSON.stringify(JSON.stringify(outputFiles))}).items():\n            gen.update_file(name, content)\n        gen.update_file('invalid/view.renderer', 'id: invalid-output\\nentrypoint: missing.tsx\\n')\n        try:\n            await gen.commit()\n        except PermissionError:\n            gen.log('expected typed partial refusal')\nif __name__ == '__main__':\n    gen.main(run)\n`;
 const files=[{name:'probe/probe.generator',content:'id: integration-probe\nname: Integration probe\nsummary: Deterministic no-provider integration fixture\nentrypoint: run.py\nmain_config: settings.set_yaml\ndraft_files: [settings.set_yaml]\naccount_requirements: {}\n'}, {name:'probe/settings.set_yaml',content:'fields: []\n'}, {name:'probe/run.py',content:script}];
 const cell=await createCell(request,channel,files);await approve(request,channel,cell.cell_id,files[0].name);
 await checked(request,'/api/runners',{runner:{id:'integration-proof',generator:'integration-probe',name:'Integration proof',description:'Synthetic local process only',prompt:'Deterministic fixture',settings:{'settings.set_yaml':{}},accounts:{},folders:[],permissions:{docker:'none',internet:'full',commands:'deny',update_packages:false},packages:{}},files:[]});
}
test('actual run renderer stays unapproved; sealed preview persists permitted writes and close flush__D09_D10_D14_D16',async({page,request})=>{
 test.setTimeout(180000);const channel = await createSeededChannel(request, `Approval run ${Date.now()}`);
 await installGenerator(request,channel);
 const work=await createCell(request,channel,[{name:'seed.txt',content:'seed'}]);await run(request,channel,work.cell_id);
 expect(await(await raw(request,channel,work.cell_id,'pkg/main.tsx')).text()).toBe(rendererSource);expect((await raw(request,channel,work.cell_id,'invalid/view.renderer')).status()).toBe(404);
 const other=await createCell(request,channel,[{name:'other.proof',content:'other cell'}]);expect(other.cell_id).not.toBe(work.cell_id);
 const unavailable=await request.get('/api/renderers/integration-renderer/source');expect(unavailable.status()).toBe(404);
 expect((await(await request.get('/api/renderers?include_disabled=true')).json()).some((r:any)=>r.id==='integration-renderer')).toBe(false);
 await displayFile(request, channel, work.cell_id, 'preview.renderer_example');
 await page.goto('/');const cell=page.locator(`[data-cell-id="${work.cell_id}"]`);await expect(cell.locator('iframe[title="Sealed renderer preview"]')).toBeAttached({timeout:90000});
 const preview=cell.frameLocator('iframe[title="Sealed renderer preview"]');await expect(preview.getByText('Real generated renderer',{exact:true})).toBeVisible({timeout:90000});const overlay=cell.locator('[title="Click to focus"]');if(await overlay.count())await overlay.click();
 await preview!.getByRole('button',{name:'Write sample.proof',exact:true}).click();await expect(preview!.locator('output')).toHaveText('sample.proof:false');expect(await(await raw(request,channel,work.cell_id,'sample.proof')).text()).toBe('original sample');
 await cell.getByRole('checkbox',{name:'Allow edits for this preview'}).check();await expect(preview!.getByRole('button',{name:'Write sample.proof',exact:true})).toBeVisible();
 await preview!.getByRole('button',{name:'Write sample.proof',exact:true}).click();await expect(preview!.locator('output')).toHaveText('sample.proof:true');
 await expect.poll(async()=>await(await raw(request,channel,work.cell_id,'sample.proof')).text()).toBe('written sample.proof');
 for(const name of ['pkg/main.tsx','blocked.renderer','blocked.generator','blocked.runner','blocked.handler','blocked.options_stack']){await preview!.getByRole('button',{name:'Write '+name,exact:true}).click();await expect(preview!.locator('output')).toHaveText(name+':false');}
 expect(await(await raw(request,channel,work.cell_id,'pkg/main.tsx')).text()).toBe(rendererSource);expect((await raw(request,channel,work.cell_id,'blocked.options_stack')).status()).toBe(404);
 // Close shows on tab hover, as for a user.
 const close=cell.getByRole('button',{name:'Close preview.renderer_example',exact:true});await close.hover({force:true});await close.click();await expect.poll(async()=>await(await raw(request,channel,work.cell_id,'exit.txt')).text()).toBe('saved on actual exit');
 await approve(request,channel,work.cell_id,'pkg/view.renderer');
 await displayFile(request, channel, other.cell_id, 'other.proof', 'integration-renderer');await page.reload();
 await expect(page.locator(`[data-cell-id="${other.cell_id}"]`).getByText('Real generated renderer',{exact:true})).toBeVisible();expect((await request.get('/api/renderers/integration-renderer/source')).status()).toBe(200);
 const catalog=await(await request.get('/api/renderers?include_disabled=true')).json();expect(catalog.find((r:any)=>r.id==='integration-renderer')).toBeTruthy();
});

test('a handler built in its work stack comes back through Add to Capabilities and is used only after Accept__D09_D10_D12_D17',async({request})=>{
 test.setTimeout(120000);const channel = await createSeededChannel(request, `Handler build ${Date.now()}`);await installGenerator(request,channel);
 const base=`/api/channels/${encodeURIComponent(channel)}`;
 const definition='id: integration-handler\nname: Integration handler\ndescription: Deterministic custom extension\ntypes:\n  - ext: .integrationcheck\nguidance: guidance.md\nexamples: []\n';
 const source=await createCell(request,channel,[{name:'custom/custom.handler',content:definition},{name:'custom/guidance.md',content:'Build only the exact deterministic verifier.'}]);
 const added=await approve(request,channel,source.cell_id,'custom/custom.handler');const id=added.cell_id;expect(id).toBeTruthy();
 expect(added.message).toBe('Added. Build it from its card in Capabilities.');
 const approvedManifest=await(await raw(request,channel,id,'custom/custom.handler')).text();
 // Build: a standard work stack opens right of Capabilities, holding a copy of the package and the brief.
 const spawned=await checked(request,`${base}/capabilities/build`,{cell_id:id,folder:'custom'});
 expect(spawned.brief_path).toBe('brief.md');
 const capabilities=await capabilitiesStack(request,channel);
 const workStack=spawned.main_stacks.find((s:any)=>s.id===spawned.stack_id);expect(workStack.stack_type).toBe('standard');
 expect(workStack.cells.map((c:any)=>c.id)).toEqual([spawned.cell_id]);expect(capabilities.cells.some((c:any)=>c.id===spawned.cell_id)).toBe(false);
 expect(await(await raw(request,channel,spawned.cell_id,'custom/custom.handler')).text()).toBe(approvedManifest);
 expect((await raw(request,channel,spawned.cell_id,'brief.md')).ok()).toBe(true);
 // Runs never start on a Capabilities cell; the work cell is where the run goes.
 const refused=await request.post(`${base}/runs`,{data:{runner:'integration-proof',cell_id:id}});expect(refused.status()).toBe(400);
 await run(request,channel,spawned.cell_id);expect(await(await raw(request,channel,spawned.cell_id,'custom/derived/verify.ts')).text()).toBe(verifier);
 expect((await raw(request,channel,id,'custom/derived/verify.ts')).status()).toBe(404);
 // Add back: the original cell is the one replacing target, and the build it carries waits for review.
 const {preview}=await previewAdd(request,channel,spawned.cell_id,'custom/custom.handler');
 expect(preview.targets.filter((t:any)=>t.replaces).map((t:any)=>t.cell_id)).toEqual([id]);
 const back=await approve(request,channel,spawned.cell_id,'custom/custom.handler',id);
 expect(back.cell_id).toBe(id);expect(back.message).toBe('Added. Review and accept its build from its card in Capabilities.');
 expect(await(await raw(request,channel,id,'custom/derived/verify.ts')).text()).toBe(verifier);
 expect(await(await raw(request,channel,id,'custom/custom.handler')).text()).not.toContain('derived:');
 const catalogBefore=await(await request.get('/api/handlers')).json();expect(catalogBefore.find((h:any)=>h.id==='integration-handler')).toMatchObject({has_derived:true,accepted:false,accepted_derived:null});
 const work=await createCell(request,channel,[{name:'value.integrationcheck',content:'wrong'}]);const validation=`${base}/cells/${work.cell_id}/validate`;
 const before=await checked(request,validation,{path:'value.integrationcheck'});expect(before.valid).toBe(true);
 const build=`${base}/cells/${id}/handler-build/`;const inspected=await checked(request,build+'inspect',{folder:'custom'});expect(inspected.compile.valid).toBe(true);
 await checked(request,build+'accept',{folder:'custom',expected_content_hash:inspected.content_hash});const after=await checked(request,validation,{path:'value.integrationcheck'});expect(after.valid).toBe(false);expect(after.error).toContain('integration exact value');
});
