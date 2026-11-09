import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {expect,test} from '@playwright/test';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

test('retained assistant layout renders the measured 200-message working transcript __F1772_F1756',async({page,request})=>{
 const compiler=await import(pathToFileURL(path.resolve(process.cwd(),'../sdk/scripts/renderer_bundle.mjs')).href);
 const candidate=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(process.cwd(),'../builtins/live/assistant')),entrypoint:'main.tsx'});
 // This focused renderer test supplies canonical compiler output. It does not
 // claim source-cache publication or the separate unmocked shipped roster gate.
 await page.route('**/api/renderers/assistant/source*',route=>route.fulfill({contentType:'application/javascript',headers:{'X-Entrypoint':'module.js'},body:candidate.code}));
 const channel = await createSeededChannel(request, `Assistant bounded profile ${Date.now()}`);

 const pointer={chat_id:'',ws_path:'',label:'Measured transcript',active_thread:'main',threads:[{id:'main',file:'threads/main.json'}]};
 const body=Array.from({length:35},(_,i)=>`Paragraph ${i}: **measured Markdown** and ordinary transcript content.`).join('\n\n');
 const messages=Array.from({length:200},(_,i)=>({role:i%2?'assistant':'user',content:`Message ${i}\n\n${body}`}));
 const {cell_id}=await importCell(request,channel,'Measured transcript',[{name:'sample.chat',content:JSON.stringify(pointer)},{name:'threads/main.json',content:JSON.stringify({messages})}]);
 await displayFile(request, channel, cell_id, 'sample.chat', 'assistant');
 await page.goto('/');const cell=page.locator(`[data-cell-id="${cell_id}"]`);await expect(cell.getByText('Message 199',{exact:false})).toBeVisible();
 await expect(cell.locator('[data-role="assistant"],[data-role="user"]')).toHaveCount(200);
 expect(await cell.locator('[data-role="assistant"]').last().evaluate(node=>getComputedStyle(node).contentVisibility)).toBe('visible');
});
