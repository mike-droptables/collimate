import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {NativeRenderer} from '../NativeRenderer';
import {useRendererStylesStore} from '@/stores/useRendererStylesStore';
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
vi.mock('@/lib/cellFileCap',async original=>({...await original<typeof import('@/lib/cellFileCap')>(),cellFileCap:async()=> 'testcap'}));
it('HTML document-relative resources use the cell capability base without a base-uri restriction __P0F20_F1832',async()=>{
 useRendererStylesStore.getState().adoptRendererStyles({'fixture-html-base':{theme:'authored',font:null,textSize:null}});
 const messages:Array<{base:string;resource:string;asset:string}>=[];
 const listener=(event:MessageEvent)=>{if(event.data?.htmlBaseTest)messages.push(event.data);};
 window.addEventListener('message',listener);
 const content=`<!doctype html><head><title>Relative resource</title></head><body><script>
 fetch('resource.txt').then(r=>r.text()).catch(error=>'REFUSED:'+error.message).then(resource=>parent.postMessage({htmlBaseTest:true,base:document.baseURI,asset:new URL('img/icon.svg',new URL('styles/main.css',document.baseURI)).href,resource},'*'));
 </script></body>`;
 const view=await render(<NativeRenderer rendererId='fixture-html-base' rendererHash='html-base' fileName='page.html' fileContent={content} cellId='html-cell' channel='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' isActiveFocus/>);
 try{
  await vi.waitFor(()=>expect(messages.length).toBeGreaterThan(0),{timeout:30000});
  const base=`${window.location.origin}/api/cellcap/testcap/channels/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/cells/html-cell/files/`;
  expect(messages.at(-1)).toMatchObject({base,resource:'cell-relative-resource',asset:base+'styles/img/icon.svg'});
  const frame=[...document.querySelectorAll('*')].map(node=>node.shadowRoot?.querySelector('iframe[title="page"]')).find(Boolean) as HTMLIFrameElement;
  expect(frame.sandbox.contains('allow-same-origin')).toBe(false);
  const doc=new DOMParser().parseFromString(frame.srcdoc,'text/html');
  expect(doc.querySelector('base')?.href).toBe(base);
  expect([...doc.querySelectorAll('meta[http-equiv]')].some(meta=>/base-uri/i.test(meta.getAttribute('content')||''))).toBe(false);
 }finally{await view.unmount();window.removeEventListener('message',listener);useRendererStylesStore.getState().adoptRendererStyles({});}
},35000);
