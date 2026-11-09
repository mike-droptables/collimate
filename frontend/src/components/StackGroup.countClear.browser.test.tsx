// The stack head's slot count, right of the last pip, never runs under the
// 'Move stack right' arrow, however many digits it has (Capabilities is the
// first stack that routinely holds 10 or more slots).
import '@/app/globals.css';
import {render} from 'vitest-browser-react';
import {expect,it,vi} from 'vitest';
import {StackGroup} from './StackGroup';
import {FocusProvider} from '@/contexts/FocusContext';
import type {Stack} from '@/lib/types';
vi.mock('./StackItem',()=>({StackItem:()=>null}));

const stackOf=(n:number):Stack=>({id:`s${n}`,name:'Research',cells:Array.from({length:n},(_,i)=>({id:`c${i}`,name:`c${i}`,files:[]}))});
const rect=(selector:string)=>document.querySelector<HTMLElement>(selector)!.getBoundingClientRect();

it.each([3,9,10,11,20,40])('Chromium: a head of %i slots keeps its count clear of the right arrow',async(n)=>{
    const stack=stackOf(n);
    const view=await render(<FocusProvider><StackGroup stack={stack} width={800} height={500} isSelected viewMode="stack" visibleTopIndex={0}
        callbacks={{onSelect(){},onClose(){},onRemoveCell(){},onUpdate(){},onSetTopSlot(){}}}/></FocusProvider>);
    try{
        await expect.poll(()=>document.querySelector<HTMLElement>('[data-stack-id]')!.getBoundingClientRect().width,{timeout:3000}).toBe(800);
        const count=rect('[data-stack-count]');
        const arrow=document.querySelector<HTMLElement>('[aria-label="Move stack right"]')!;
        const icon=arrow.querySelector('svg')!.getBoundingClientRect();
        expect(document.querySelector('[data-stack-count]')!.textContent).toBe(String(n));
        // The count ends clear of the arrow's glyph, with the same gap a
        // one-digit count has (it ends at the arrow's box).
        expect(count.right).toBeLessThan(icon.left-3);
        expect(count.right).toBeLessThan(arrow.getBoundingClientRect().left+1);
        // And it still sits right of the last pip.
        expect(count.left).toBeGreaterThan(rect(`[data-scrub-slot="${n-1}"]`).left);
    }finally{view.unmount();}
});
