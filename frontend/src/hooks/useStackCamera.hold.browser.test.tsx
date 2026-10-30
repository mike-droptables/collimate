import {act} from 'react';
import {render} from 'vitest-browser-react';
import {afterEach, expect, it} from 'vitest';
import {useStackCamera} from './useStackCamera';
import type {StackLayoutResult} from './useStackLayout';
import type {Stack} from '@/lib/types';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {defaultCommandPane} from '@/stores/collimate/commandPaneSlice';

afterEach(() => { useCollimateStore.setState({commandPane: defaultCommandPane()}); });

it('commits the current running-spring position after a quick move hold __F0616', async () => {
    const target={current:0};
    const layout={viewSize:{width:1000,height:800},GAP:14,positions:[],widths:[],arrowWorldX:null,cellPendingMoveWorldX:null,overviewTarget:{x:0,y:0,zoom:1},sceneSwapNonce:0} as unknown as StackLayoutResult;
    let camera!: ReturnType<typeof useStackCamera>;
    function Probe({hold}:{hold:boolean}) {
        camera=useStackCamera({layout,stacks:[],selectedId:null,isZoomedOut:false,topology:'stable',pendingMove:null,centerRequested:0,edgeAlignRequested:null,cameraHold:hold,widthDragActive:false,cameraTargetRef:target,rootDivRef:{current:null}});
        return null;
    }
    const view=await render(<Probe hold={false}/>);
    try {
        act(()=>camera.commitCameraTarget(600));
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        expect(camera.cameraXMV.isAnimating()).toBe(true);
        await view.rerender(<Probe hold/>);
        const held=camera.cameraXMV.get();
        await new Promise(resolve=>setTimeout(resolve,60));
        const current=camera.cameraXMV.get();
        expect(current).toBeGreaterThan(held);
        await view.rerender(<Probe hold={false}/>);
        // Rerender waits for a browser commit: the old spring may advance
        // before that commit, but release must never rewind or chase its old goal.
        const released=camera.cameraXMV.get();
        expect(released).toBeGreaterThanOrEqual(current);
        expect(released).toBeLessThan(600);
        expect(target.current).toBeCloseTo(released,4);
        expect(camera.cameraXMV.isAnimating()).toBe(false);
    } finally {await view.unmount();}
});

it('releasing the hold with the Commands pane open keeps the released position, never the old goal (K01)', async () => {
    const target={current:0};
    // One 300px stack at world 0 in a 3000px canvas: it clears a right pane
    // anywhere on the way to 600, so the pane adds no slide of its own.
    const layout={viewSize:{width:3000,height:800},GAP:14,positions:[0],widths:[300],arrowWorldX:null,cellPendingMoveWorldX:null,gridTarget:{x:0,y:0,zoom:1},sceneSwapNonce:0} as unknown as StackLayoutResult;
    const stacks=[{id:'s1',name:'Frame',cells:[]}] as Stack[];
    let camera!: ReturnType<typeof useStackCamera>;
    function Probe({hold}:{hold:boolean}) {
        camera=useStackCamera({layout,stacks,selectedId:'s1',isZoomedOut:false,topology:'stable',pendingMove:null,centerRequested:0,edgeAlignRequested:null,cameraHold:hold,widthDragActive:false,cameraTargetRef:target,rootDivRef:{current:null}});
        return null;
    }
    const view=await render(<Probe hold={false}/>);
    try {
        act(()=>{ useCollimateStore.getState().setCommandPaneSide('right'); useCollimateStore.getState().openCommandPane(); });
        act(()=>camera.commitCameraTarget(600));
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        expect(camera.cameraXMV.isAnimating()).toBe(true);
        await view.rerender(<Probe hold/>);
        await new Promise(resolve=>setTimeout(resolve,60));
        await view.rerender(<Probe hold={false}/>);
        const released=camera.cameraXMV.get();
        expect(released).toBeLessThan(600);
        // Effect 1 reruns on the hold's release with the pane open: it must
        // build from the released position, not chase 600.
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        expect(target.current).toBeCloseTo(released,4);
        expect(camera.cameraXMV.get()).toBeCloseTo(released,4);
    } finally {await view.unmount();}
});
