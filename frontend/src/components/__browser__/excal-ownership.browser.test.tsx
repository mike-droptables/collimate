import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page} from 'vitest/browser';
import {NativeRenderer} from '../NativeRenderer';
import {flushRendererEditors} from '@/lib/rendererFlush';
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
it('delivers a drawing immediately and does not repeat it during host flush __F1803',async()=>{
 const writes:string[]=[];
 const props={rendererId:'fixture-excal-ownership',rendererHash:'excal-ownership',fileName:'scene.excalidraw',fileContent:JSON.stringify({elements:[],appState:{theme:'light'},files:{}}),isActiveFocus:true,onContentChange:(body:string)=>writes.push(body)};
 const view=await render(<NativeRenderer {...props}/>);
 try{
  await page.getByRole('button',{name:'Queue drawing edit'}).click();
  await vi.waitFor(()=>expect(writes).toHaveLength(1));
  expect(JSON.parse(writes[0]).elements).toEqual([{id:'pending'}]);
  await view.rerender(<NativeRenderer {...props} fileContent={JSON.stringify({elements:[{id:'authoritative'}],appState:{theme:'dark'},files:{}})}/>);
  await flushRendererEditors(undefined,'scene.excalidraw');
  await new Promise(resolve=>setTimeout(resolve,600));
  expect(writes).toHaveLength(1);
 }finally{await view.unmount();}
},15000);
