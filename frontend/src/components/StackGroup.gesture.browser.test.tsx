import {act} from 'react';
import {render} from 'vitest-browser-react';
import {expect,it,vi} from 'vitest';
import {page} from 'vitest/browser';
import {StackGroup} from './StackGroup';
import {FocusProvider} from '@/contexts/FocusContext';
import {DEFAULT_MIN_CELL_SIZE, useSpatialStore} from '@/stores/useSpatialStore';
import {maxStackWidthFor} from '@/hooks/useStackLayout';
import {CENTER_CLICK_DELAY_MS} from '@/hooks/useCenterPanGesture';
vi.mock('./StackItem',()=>({StackItem:()=>null}));

it("Chromium: the head's empty space recentres on a click after the double-click window, pans on a drag and toggles full width on a double-click (C06, polish)",async()=>{
    let moved=false;
    const onCenterStack=vi.fn();
    const onCenterPanMove=vi.fn(()=>{moved=true;});
    // Wide enough that the default width is below full width.
    await page.viewport(1200,600);
    useSpatialStore.setState({minCellSize:DEFAULT_MIN_CELL_SIZE,stackWidths:{},isMaxWidth:false});
    const view=await render(<FocusProvider><StackGroup stack={{id:'s',name:'',cells:[{id:'a',name:'a',files:[]},{id:'b',name:'b',files:[]}]}} width={800} height={500} isSelected viewMode="stack" callbacks={{onSelect(){},onClose(){},onRemoveCell(){},onUpdate(){},onSetTopSlot(){},onCenterStack,onCenterPanStart(){moved=false;},onCenterPanMove,onCenterPanEnd(){return moved;}}}/></FocusProvider>);
    try {
        const head=document.querySelector<HTMLElement>('[data-stack-head]')!;
        // Controlled PointerEvents are not trusted OS input, so Chromium does
        // not create native pointer capture. The production handlers and DOM
        // click/double-click order are otherwise exercised unchanged.
        head.setPointerCapture=()=>{};head.hasPointerCapture=()=>false;
        const pointer=(type:string,x:number)=>act(()=>{head.dispatchEvent(new PointerEvent(type,{bubbles:true,button:0,buttons:type==='pointerup'?0:1,pointerId:7,clientX:x}));});
        const click=(type:string,detail:number,x=100)=>act(()=>{head.dispatchEvent(new MouseEvent(type,{bubbles:true,button:0,detail,clientX:x}));});
        const windowPasses=()=>act(()=>new Promise(resolve=>setTimeout(resolve,CENTER_CLICK_DELAY_MS+50)));
        // A click recentres once the double-click window has passed (polish).
        pointer('pointerdown',100);pointer('pointerup',100);click('click',1);
        expect(onCenterStack).not.toHaveBeenCalled();
        await windowPasses();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
        pointer('pointerdown',100);
        await new Promise(resolve=>setTimeout(resolve,80));
        pointer('pointermove',140);pointer('pointerup',140);click('click',1);
        expect(onCenterPanMove).toHaveBeenCalledTimes(1);
        expect(onCenterStack).toHaveBeenCalledTimes(1);
        // A double-click whose second press was a pan leaves the width alone.
        pointer('pointerdown',140);pointer('pointerup',140);click('click',1,140);
        pointer('pointerdown',140);pointer('pointermove',190);pointer('pointerup',190);click('click',2,190);click('dblclick',2,190);
        expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE);
        // A still double-click: full width, then back to the default.
        pointer('pointerdown',140);pointer('pointerup',140);click('click',1,140);
        pointer('pointerdown',140);pointer('pointerup',140);click('click',2,140);click('dblclick',2,140);
        expect(useSpatialStore.getState().minCellSize).toBe(maxStackWidthFor(window.innerWidth));
        pointer('pointerdown',140);pointer('pointerup',140);click('click',1,140);
        pointer('pointerdown',140);pointer('pointerup',140);click('click',2,140);click('dblclick',2,140);
        expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE);
        // No double-click's first click recentred (polish).
        await windowPasses();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    }finally{await view.unmount();}
});
