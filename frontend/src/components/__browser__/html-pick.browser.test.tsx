import {it,expect,vi} from 'vitest';
import {page,userEvent} from 'vitest/browser';
import {render} from 'vitest-browser-react';
import {NativeRenderer} from '../NativeRenderer';
import {useRendererStylesStore} from '@/stores/useRendererStylesStore';
import {annotationResponders,requestAnnotation} from '@/lib/rendererAnnotation';
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
vi.mock('@/lib/cellFileCap',async original=>({...await original<typeof import('@/lib/cellFileCap')>(),cellFileCap:async()=> 'testcap'}));

// The SDK's html renderer answering a pick through the host's registry
// (lib/rendererAnnotation): the request reaches the renderer's
// useAnnotationRequest, the document view's picker arms in the page, and the
// user's click there attaches that element while the page's own handlers
// never hear it. Esc in the page abandons the pick, and a new document
// (the file changed mid-pick) re-arms it.

const CHANNEL='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const page1=(first:string)=>`<!doctype html><body><section id="sec"><ul><li>${first}</li><li>Second item</li></ul></section>
<script>document.addEventListener('click',()=>parent.postMessage({pageClick:true},'*'));</script></body>`;
const CSS='#sec > ul:nth-of-type(1) > li:nth-of-type(2)';
const pageFrame=()=>[...document.querySelectorAll('*')].map(node=>node.shadowRoot?.querySelector('iframe[title="page"]')).find(Boolean) as HTMLIFrameElement|undefined;
const settle=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));

it('a pick in the html page answers the clicked element, and Esc in the page abandons it',async()=>{
 useRendererStylesStore.getState().adoptRendererStyles({'fixture-html-base':{theme:'authored',font:null,textSize:null}});
 const pageClicks:unknown[]=[];let ready=0;
 const listener=(event:MessageEvent)=>{
  if(event.data?.pageClick)pageClicks.push(event.data);
  if(event.data?.type==='collimate:pick-ready'&&event.source===pageFrame()?.contentWindow)ready++;
 };
 window.addEventListener('message',listener);
 const element=(content:string)=><NativeRenderer rendererId='fixture-html-base' rendererHash='html-pick' fileName='page.html' fileContent={content} cellId='pick-cell' channel={CHANNEL} isActiveFocus/>;
 const view=await render(element(page1('One')));
 try{
  await vi.waitFor(()=>expect(annotationResponders('pick-cell','page.html').some(responder=>responder.capable())).toBe(true),{timeout:30000});
  await vi.waitFor(()=>expect(ready).toBeGreaterThan(0),{timeout:15000});
  const clickSecond=async()=>{
   await settle(300);
   await page.frameLocator(page.elementLocator(pageFrame()!)).getByText('Second item').click();
  };

  const picked=requestAnnotation('pick-cell','page.html','pick');
  await clickSecond();
  const outcome=await picked;
  expect(outcome).toMatchObject({status:'ok',path:'page.html',rendererId:'fixture-html-base',
   context:{label:`page.html › ${CSS}`,anchor:{css:CSS,id:'sec',snippet:'Second item'}}});
  if(outcome.status!=='ok')return;
  expect(outcome.context.text.split('\n')[0]).toBe(`Element \`<li>\` in \`page.html\` (css \`${CSS}\`, id \`sec\`):`);
  expect(outcome.context.text).toContain('> Second item');
  expect(outcome.context.text).toContain('<li>Second item</li>');
  await settle(200);
  expect(pageClicks,'the page never hears the pick\'s click').toEqual([]);

  const abandoned=requestAnnotation('pick-cell','page.html','pick');
  await settle(300);
  await userEvent.keyboard('{Escape}');
  expect(await abandoned).toEqual({status:'cancelled'});

  const rearmed=requestAnnotation('pick-cell','page.html','pick');
  await settle(300);
  const before=ready;
  await view.rerender(element(page1('First')));
  await vi.waitFor(()=>expect(ready).toBeGreaterThan(before),{timeout:15000});
  await clickSecond();
  expect(await rearmed).toMatchObject({status:'ok',context:{label:`page.html › ${CSS}`}});
  await settle(200);
  expect(pageClicks).toEqual([]);
 }finally{await view.unmount();window.removeEventListener('message',listener);useRendererStylesStore.getState().adoptRendererStyles({});}
},60000);
