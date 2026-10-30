/** Isolated pre-vendor proof: install exact SDK source through ordinary user approval. */
import {expect,type APIRequestContext} from '@playwright/test';
import {readFileSync,readdirSync} from 'node:fs';
import path from 'node:path';
import {importCell} from './persistenceFixtures';

export async function installCandidate(request:APIRequestContext,channel:string,id:string){
 if(process.env.COLLIMATE_RENDERER_CANDIDATE_SOURCE!=='1')return;
 const folder=path.resolve(__dirname,`../../../content/renderers/${id==='monaco'?'documents':'visual'}/${id}`);
 const files=readdirSync(folder,{recursive:true,withFileTypes:true}).filter(entry=>entry.isFile()).map(entry=>({name:'candidate/'+path.relative(folder,path.join(entry.parentPath,entry.name)),content:readFileSync(path.join(entry.parentPath,entry.name),'utf8')}));
 const {cell_id}=await importCell(request,channel,`Canonical ${id} candidate`,files);
 const catalog=await (await request.get('/api/renderers?include_disabled=true')).json();
 const record=catalog.find((entry:{id:string})=>entry.id===id);expect(record).toBeTruthy();
 // The shipped renderer's cell sits on the channel's Capabilities stack: the candidate replaces it there.
 const state=await (await request.get('/api/state')).json();
 const target=state.channels.find((entry:{id:string})=>entry.id===channel);expect(target).toBeTruthy();
 const capabilities=[...target.main_stacks,...target.nav_stacks].find((entry:{stack_type:string})=>entry.stack_type==='capabilities');
 expect(capabilities.cells.some((cell:{id:string})=>cell.id===record.cell_id)).toBe(true);
 const candidate={source_cell_id:cell_id,definition_path:files.find(file=>file.name.endsWith('.renderer'))!.name,target_cell_id:record.cell_id};
 const preview=await request.post(`/api/channels/${encodeURIComponent(channel)}/packages/preview`,{data:candidate});expect(preview.ok(),await preview.text()).toBe(true);const info=await preview.json();
 const approval=await request.post(`/api/channels/${encodeURIComponent(channel)}/packages/add`,{data:{...candidate,expected_content_hash:info.content_hash,expected_target_revision:info.target_revision,replace:true}});expect(approval.ok(),await approval.text()).toBe(true);
}
