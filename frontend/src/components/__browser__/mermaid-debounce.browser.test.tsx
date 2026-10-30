import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page} from 'vitest/browser';
import {NativeRenderer} from '../NativeRenderer';
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
const preview=()=>[...document.querySelectorAll('*')].map(node=>node.shadowRoot?.querySelector('svg')).find(Boolean);
it('keeps the last good SVG when syntax fails and uses fresh SVG ids __F1799',async()=>{
 const mermaidUrl='https://esm.sh/mermaid@12.2.0?target=es2022';
 const mermaid=(await import(/* @vite-ignore */ mermaidUrl)).default;
 const calls=vi.spyOn(mermaid,'render'); // Observe the real renderer; keep its implementation.
 const view=await render(<NativeRenderer rendererId="fixture-mermaid-debounce" rendererHash="mermaid-debounce" fileName="diagram.mmd" fileContent="graph TD; A[P3First] --> B[Done]" isActiveFocus/>);
 try{
  await vi.waitFor(()=>expect(preview()?.textContent).toContain('P3First'),{timeout:30000});
  const first=preview()!.id;
  const initialCalls=calls.mock.calls.length;
  const field=page.getByRole('textbox').element() as HTMLTextAreaElement;
  const setValue=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')!.set!;
  for(const text of ['graph TD; A[Transient] --> B[Done]','graph TD; A[P3Second] --> B[Done]']){
   setValue.call(field,text);field.dispatchEvent(new Event('input',{bubbles:true}));
  }
  expect(preview()!.id).toBe(first);
  expect(calls.mock.calls.length).toBe(initialCalls);
  await vi.waitFor(()=>expect(preview()?.textContent).toContain('P3Second'),{timeout:10000});
  expect(calls.mock.calls.length).toBe(initialCalls+1);
  expect(preview()!.id).not.toBe(first);
  const last=preview()!.outerHTML;
  await page.getByRole('textbox').fill('graph ???');
  await expect.element(page.getByRole('alert')).not.toBeEmptyDOMElement();
  expect(preview()?.outerHTML).toBe(last);
  expect(document.querySelector('svg[aria-roledescription="error"]')).toBeNull();
 }finally{await view.unmount();calls.mockRestore();}
},45000);
