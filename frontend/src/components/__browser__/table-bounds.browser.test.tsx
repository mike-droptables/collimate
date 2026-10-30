import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page} from 'vitest/browser';
import {NativeRenderer} from '../NativeRenderer';
vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));
const csv=(count:number)=>'name,value\n'+Array.from({length:count},(_,i)=>`row${String(i).padStart(5,'0')},${count-i}`).join('\n');
const rows=()=>[...document.querySelectorAll('*')].map(node=>node.shadowRoot?.querySelector('tbody')).find(Boolean)?.querySelectorAll('tr').length||0;
it('caps table DOM, reveals more explicitly, and resets limits for filter and changed content __F1833_F1834',async()=>{
 const props={rendererId:'fixture-table-bounds',rendererHash:'table-bounds',fileName:'data.csv',fileContent:csv(1205),isActiveFocus:true};
 const view=await render(<NativeRenderer {...props}/>);
 try{
  await expect.element(page.getByText('1205 rows',{exact:true})).toBeInTheDocument();
  expect(rows()).toBe(1000);
  await page.getByRole('button',{name:'Show 205 more'}).click();
  expect(rows()).toBe(1205);
  await page.getByPlaceholder('filter rows…').fill('row01199');
  await vi.waitFor(()=>expect(rows()).toBe(1));
  await expect.element(page.getByText('row01199',{exact:true})).toBeInTheDocument();
  await page.getByPlaceholder('filter rows…').fill('');
  await vi.waitFor(()=>expect(rows()).toBe(1000));
  await view.rerender(<NativeRenderer {...props} fileContent={csv(50000)}/>);
  await expect.element(page.getByText('50000 rows',{exact:true})).toBeInTheDocument();
  expect(rows()).toBe(1000);
  await page.getByRole('columnheader',{name:'value'}).click();
  await expect.element(page.getByText('row49999',{exact:true})).toBeInTheDocument();
  expect(rows()).toBe(1000);
 }finally{await view.unmount();}
},35000);
