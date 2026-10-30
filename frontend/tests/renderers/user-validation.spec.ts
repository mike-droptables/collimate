import { displayFile, putTextFiles, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {expect,test,type APIRequestContext} from '@playwright/test';

// User validation of a channel's own file type: a user handler for
// `.validation_html` (no built-in surface) whose accepted verifier refuses
// script, and, for the editor test, a user renderer that claims the type.
const FILE='report.validation_html';
const verifier='export default function(input){return input.text.includes("<script") ? {valid:false,error:"script elements are not allowed"} : {valid:true};}';
const editorSource=`import React from 'https://esm.sh/react@19.3.1?target=es2022';
import {defineRenderer,useCollimateFile} from '/api/sdk/react.js';
function Editor(){const {content,saveContent}=useCollimateFile();return React.createElement('textarea',{'aria-label':'Validation editor',value:content,onChange:e=>saveContent(e.target.value)});}export default defineRenderer({component:Editor});`;

async function checked(request:APIRequestContext,url:string,data:unknown){const r=await request.post(url,{data});expect(r.ok(),await r.text()).toBe(true);return r.json();}

/** Add the package at `definition` of a new cell holding `files` to the
 *  channel's Capabilities stack, in a new cell; returns that cell and its
 *  folder. */
async function admit(request:APIRequestContext,channel:string,name:string,files:{name:string;content:string}[],definition:string){
 const base=`/api/channels/${encodeURIComponent(channel)}`;
 const {cell_id:sourceId}=await importCell(request,channel,name,files);
 const args={source_cell_id:sourceId,definition_path:definition,target_cell_id:null};
 const prepared=await checked(request,base+'/packages/preview',args);
 const added=await checked(request,base+'/packages/add',{...args,expected_content_hash:prepared.content_hash,expected_target_revision:prepared.target_revision,replace:false});
 return {cellId:added.cell_id as string,folder:prepared.folder as string};
}

/** The user handler for `.validation_html`, its verifier built and accepted. */
async function installValidationHandler(request:APIRequestContext,channel:string){
 const definition='id: validation-fixture\nname: Validation fixture\ndescription: Refuses script in validation documents\ntypes:\n  - ext: .validation_html\nguidance: guidance.md\nexamples: []\n';
 const {cellId,folder}=await admit(request,channel,'Validation handler candidate',[{name:'fixture/fixture.handler',content:definition},{name:'fixture/guidance.md',content:'Refuse any script element.'}],'fixture/fixture.handler');
 await putTextFiles(request,channel,cellId,[{name:`${folder}/derived/verify.ts`,content:verifier}]);
 const build=`/api/channels/${encodeURIComponent(channel)}/cells/${cellId}/handler-build/`;
 const inspected=await checked(request,build+'inspect',{folder});expect(inspected.compile.valid,JSON.stringify(inspected)).toBe(true);
 await checked(request,build+'accept',{folder,expected_content_hash:inspected.content_hash});
 const catalog=await(await request.get('/api/handlers')).json();
 expect(catalog.find((h:{id:string})=>h.id==='validation-fixture')).toMatchObject({builtin:false,accepted:true});
}

test('local editor mutation completion refreshes the renderer banner without a focus event __D10',async({page,request})=>{
 const channel = await createSeededChannel(request, `Local validation ${Date.now()}`);const base=`/api/channels/${encodeURIComponent(channel)}`;
 await installValidationHandler(request,channel);
 await admit(request,channel,'Validation editor candidate',[{name:'editor/editor.renderer',content:'id: validation-editor\nname: Validation editor\nsummary: Immediate editor save fixture\nentrypoint: main.tsx\nembed: native\nextensions: [.validation_html]\n'},{name:'editor/main.tsx',content:editorSource}],'editor/editor.renderer');
 const {cell_id:id}=await importCell(request,channel,'Local validation feedback',[{name:FILE,content:'<section id="summary">ok</section>'}]);
 await displayFile(request, channel, id, FILE, 'validation-editor');
 await page.goto('/');const cell=page.locator(`[data-cell-id="${id}"]`);
 const overlay=cell.locator('[title="Click to focus"]');if(await overlay.count())await overlay.click();
 const editor=cell.getByRole('textbox',{name:'Validation editor'});await expect(editor).toBeVisible();
 // A passing validation is silent: no banner on valid content.
 await expect(cell.getByTestId('validation-banner')).toHaveCount(0);
 const persisted=page.waitForResponse(response=>response.url().endsWith(`/cells/${id}/files/mutate`)&&response.request().method()==='POST'&&response.request().postDataJSON()?.mutations?.some((mutation:{path:string;content_text?:string})=>mutation.path===FILE&&mutation.content_text==='<script>invalid local edit</script>'));
 await editor.fill('<script>invalid local edit</script>');
 const mutation=await persisted;expect(mutation.ok(),await mutation.text()).toBe(true);
 expect((await mutation.json()).outcome).toBe('accepted');
 // The accepted mutation must refresh validation without a focus event.
 await expect(cell.getByTestId('validation-banner')).toContainText('Validation failed: script elements are not allowed',{timeout:1500});
 expect(await(await request.get(`${base}/cells/${id}/files/${FILE}`)).text()).toBe('<script>invalid local edit</script>');
});

test('actual user mutations save invalid bytes and show current advisory renderer verdicts __D10',async({page,request})=>{
 const channel = await createSeededChannel(request, `User validation ${Date.now()}`);
 await installValidationHandler(request,channel);
 const base=`/api/channels/${encodeURIComponent(channel)}`;
 const {cell_id:id}=await importCell(request,channel,'Validation feedback',[{name:FILE,content:'<section id="summary">ok</section>'}]);const cellBase=`${base}/cells/${id}`;
 const invalid='<script>bad()</script>';
 await putTextFiles(request,channel,id,[{name:FILE,content:invalid}]);
 expect(await(await request.get(cellBase+'/files/'+FILE)).text()).toBe(invalid);
 await displayFile(request, channel, id, FILE);
 await page.goto('/');const cell=page.locator(`[data-cell-id="${id}"]`);
 const banner=cell.getByTestId('validation-banner');
 await expect(banner).toContainText('Validation failed: script elements are not allowed');
 await expect(banner).toHaveAttribute('role','alert');
 const valid='<section id="summary">corrected</section>';
 await putTextFiles(request,channel,id,[{name:FILE,content:valid}]);
 // The real observer refreshes when the app regains focus after another client saves.
 await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
 // A passing validation is silent: the alert goes away.
 await expect(banner).toHaveCount(0);
 expect(await(await request.get(cellBase+'/files/'+FILE)).text()).toBe(valid);
});
