import { fileFixture } from '@/test/fileFixtures';
import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {page} from 'vitest/browser';
import {TabStrip} from '../cellEditor/TabStrip';
import {useCellValidation} from '@/hooks/useCellValidation';
import {fetchCellValidation} from '@/lib/definitionApi';
import type {CellValidation} from '@/lib/definitionApi';
import type {ContentLeaf} from '@/stores/collimate/types';
import '@/app/globals.css';

vi.mock('@/lib/definitionApi',()=>({fetchCellValidation:vi.fn()}));
const leaf:ContentLeaf={type:'leaf',id:'leaf',activeTabId:'file',tabs:[{kind:'file',id:'file',path:'note.txt'}]};
const files=[fileFixture('note.txt', '{}', {language:'json', timestamp:'t'})];
const saved={};
const verdict:CellValidation={cell_id:'cell',source_version_id:'v',source_revision:'r',pending:false,verdicts:{'note.txt':{valid:true}}};

it('focus refresh keeps Close under the pointer and still awaits the save barrier __P3F10',async()=>{
 vi.mocked(fetchCellValidation).mockResolvedValueOnce(verdict).mockImplementation(()=>new Promise(()=>{}));
 let finish!:()=>void;
 const flush=vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;}));
 const close=vi.fn();
 function Harness(){
  useCellValidation('channel','cell',files,true,saved);
  return <div onMouseDownCapture={()=>window.dispatchEvent(new Event('focus'))} style={{width:700}}>
   <TabStrip cellId="cell" pinnedPath={null} leaf={leaf}
    onLayoutChange={()=>{}} onBeforeStructuralChange={flush} onToggleMaximized={()=>{}} onCloseFileTab={close}/>
  </div>;
 }
 const view=await render(<Harness/>);
 try{
  await vi.waitFor(()=>expect(fetchCellValidation).toHaveBeenCalledTimes(1));
  expect(document.querySelector('[data-testid="tab-verdict-slot"]')).toBeNull();
  const button=document.querySelector<HTMLButtonElement>('[aria-label="Close note.txt"]')!;
  const before=button.getBoundingClientRect().x;
  // Close takes pointer events only under a hovered tab, as for a user.
  await page.elementLocator(button.closest('[data-tab-id]')!).hover();
  await page.getByRole('button',{name:'Close note.txt',exact:true}).click();
  expect(document.querySelector('[data-testid="tab-verdict-slot"]')).toBeNull();
  expect(button.getBoundingClientRect().x).toBe(before);
  expect(flush).toHaveBeenCalledTimes(1);
  expect(close).not.toHaveBeenCalled();
  finish();
  await vi.waitFor(()=>expect(close).toHaveBeenCalledWith('note.txt'));
 }finally{await view.unmount();}
});
