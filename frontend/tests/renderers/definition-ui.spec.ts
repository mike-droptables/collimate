import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import {expect,test} from '@playwright/test';

test('actual app definition form saves selected bytes and the Files panel adds the inspected package to Capabilities__D13_D15_D16 (P03)',async({page,request})=>{
 const channel = await createSeededChannel(request, `Definition UI ${Date.now()}`);

 const files=[
  {name:'candidate/view.renderer',content:'id: candidate-view\nname: Candidate view\nsummary: UI proof\nentrypoint: main.tsx\nembed: native\nextensions: [.proof]\n'},
  {name:'candidate/main.tsx',content:"import {defineRenderer} from '/api/sdk/react.js'; function Candidate(){return null;} export default defineRenderer({component:Candidate});"},
  {name:'sample.proof',content:'sample exact bytes'},
 ];
 const {cell_id}=await importCell(request,channel,'Candidate work',files);
 await displayFile(request, channel, cell_id, files[0].name);
 await page.goto('/');const cell=page.locator(`[data-cell-id="${cell_id}"]`);await expect(cell).toBeVisible();
 const form=cell.getByRole('textbox',{name:'name',exact:true});await expect(form).toHaveValue('Candidate view');await form.fill('Edited candidate');
 const saved=page.waitForResponse(r=>r.url().includes(`/cells/${cell_id}`)&&r.request().method()!=='GET');
 await cell.getByRole('button',{name:'Save definition',exact:true}).click();
 expect((await saved).ok()).toBeTruthy();
 // The file's options in the Files panel: Info names the file's handler (a definition's is the app's built-in one).
 const hidden=cell.getByRole('button',{name:'Show files',exact:true});if(await hidden.count())await hidden.click();
 await cell.getByRole('button',{name:'Find file',exact:true}).click();
 const panel=cell.getByRole('dialog',{name:'Files'});
 await panel.getByRole('combobox',{name:'Find file'}).fill('*.renderer');
 await panel.getByRole('option').filter({hasText:'candidate/view.renderer'}).dblclick();
 await panel.getByRole('option',{name:'Info',exact:true}).click();
 await expect(panel.getByRole('heading',{name:'Handler'})).toBeVisible();
 await expect(panel.getByRole('button',{name:/built-in/})).toBeVisible();
 await panel.getByRole('button',{name:'Back',exact:true}).click();
 // Add to Capabilities, first in the options, reads the saved definition, so the saved name heads the step.
 const preview=page.waitForResponse(r=>r.url().endsWith('/packages/preview'));
 await panel.getByRole('option',{name:'Add ‘Edited candidate’ to Capabilities…'}).click();
 const previewResponse=await preview;expect(previewResponse.ok(),await previewResponse.text()).toBeTruthy();
 await expect(panel.getByRole('button',{name:'New cell',exact:true})).toHaveAttribute('aria-pressed','true');
 await expect(panel.getByText('Edited candidate',{exact:false}).last()).toBeVisible();
 await expect(panel.getByRole('listitem').filter({hasText:'main.tsx'})).toBeVisible();
 const add=page.waitForResponse(r=>r.url().endsWith('/packages/add')&&r.request().method()==='POST');
 await panel.getByRole('button',{name:'Confirm',exact:true}).click();
 const result=await add;expect(result.ok(),await result.text()).toBeTruthy();const payload=result.request().postDataJSON();expect(payload.expected_content_hash).toMatch(/^[a-f0-9]{64}$/);expect(payload.expected_target_revision).toBeTruthy();
 const catalog=await request.get('/api/renderers?include_disabled=true');const records=await catalog.json();expect(records.find((r:{id:string})=>r.id==='candidate-view')?.name).toBe('Edited candidate');
 await expect(panel.getByRole('status')).toContainText('Added');
});

test('actual options definition viewer displays the SDK literal-root mapping__D13',async({page,request})=>{
 const channel = await createSeededChannel(request, `Options UI ${Date.now()}`);

 const files=[{name:'nested/choices.options_stack',content:'options:\n  - name: Literal root\n    root: a[1]\n    files: ["**", "../brief.md"]\n'},
  {name:'nested/a[1]/x.bin',content:'exact'}, {name:'nested/brief.md',content:'brief'}, {name:'loose.txt',content:'kept'}];
 const {cell_id}=await importCell(request,channel,'Options work',files);
 await displayFile(request, channel, cell_id, files[0].name);
 const mapped=page.waitForResponse(r=>r.url().includes('/options-map?'));
 await page.goto('/');const mapping=await mapped;expect(mapping.ok(),await mapping.text()).toBeTruthy();
 const value=await mapping.json();expect(value.options[0].files).toEqual({'x.bin':'nested/a[1]/x.bin','brief.md':'nested/brief.md'});
 const cell=page.locator(`[data-cell-id="${cell_id}"]`);await expect(cell.getByText('x.bin ← nested/a[1]/x.bin',{exact:true})).toBeVisible();await expect(cell.getByText('brief.md ← nested/brief.md',{exact:true})).toBeVisible();await expect(cell.getByRole('listitem').filter({hasText:'loose.txt'})).toBeVisible();
});
