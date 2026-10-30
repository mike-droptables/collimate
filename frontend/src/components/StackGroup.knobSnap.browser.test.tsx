// The stack head's selection motion in Chromium: a new front cell slides the
// knob to its pip with the magnet snap (a gentle start, then a hard pull into
// place, no overshoot), and Alt-↓ plays the very same motion as a front set
// directly, because both write the scene's visibleTopIndices that StackGroup
// reads. Samples the knob column's computed `left` frame by frame.
//
// The app's stylesheet makes the group `relative`, the head's containing
// block, so the head is the group's width; without it the head's box would
// follow whatever ancestor happens to be positioned (the group only while
// its own animation hints `will-change`, lib/motionLayers).
import '@/app/globals.css';
import {act} from 'react';
import {render} from 'vitest-browser-react';
import {expect,it,vi} from 'vitest';
import {StackGroup} from './StackGroup';
import {FocusProvider,type FocusContextType} from '@/contexts/FocusContext';
import {useSceneActions} from '@/hooks/useSceneActions';
import {executeSceneActions} from '@/hooks/sceneKeyboardEffects';
import {SHORTCUT_IDS} from '@/lib/keybindings/ids';
import type {Channel,Stack} from '@/lib/types';
vi.mock('./StackItem',()=>({StackItem:()=>null}));

const STACKS:Stack[]=[{id:'s',name:'Research',cells:[{id:'a',name:'a',files:[]},{id:'b',name:'b',files:[]},{id:'c',name:'c',files:[]}]}];
const CHANNEL:Channel={id:'11111111-1111-4111-8111-111111111111',name:'chan',nav_stacks:[],main_stacks:STACKS,nav_selection_id:null,main_selection_id:null};
const FOCUS={setFocusArea(){},setSelectedWindowId(){},setMainFocusMode(){},selectedWindowId:'s'} as unknown as FocusContextType;
const performAction=(p:Promise<unknown>)=>p;

type Actions=ReturnType<typeof useSceneActions>['actions'];
function Scene({onActions}:{onActions:(a:Actions)=>void}){
    const scene=useSceneActions({performAction,focusState:FOCUS,mainStacks:STACKS,activeChannel:CHANNEL,isZoomedOut:false});
    onActions(scene.actions);
    return <FocusProvider><StackGroup stack={STACKS[0]} width={800} height={500} isSelected viewMode="stack"
        visibleTopIndex={scene.visibleTopIndices.s??0}
        callbacks={{onSelect(){},onClose(){},onRemoveCell(){},onUpdate(){},onSetTopSlot:slot=>scene.actions.winSetTopSlot('s',slot)}}/></FocusProvider>;
}

const column=()=>document.querySelector<HTMLElement>('[data-scrub-knob-column]')!;
const left=()=>Number.parseFloat(getComputedStyle(column()).left);
const frame=()=>new Promise<number>(resolve=>requestAnimationFrame(resolve));
const group=()=>document.querySelector<HTMLElement>('[data-stack-id]')!;

/** Change the front, then sample the knob's left each frame until it rests. */
async function sampleSlide(change:()=>void){
    const from=left();
    const t0=performance.now();
    act(change);
    const samples:{t:number;x:number}[]=[];
    while(performance.now()-t0<600){
        await frame();
        samples.push({t:performance.now()-t0,x:left()});
    }
    const to=samples[samples.length-1].x;
    const progress=samples.map(s=>({t:s.t,p:(s.x-from)/(to-from)}));
    const arrived=progress.find(s=>Math.abs(1-s.p)<0.005)!.t;
    return {from,to,progress,arrived};
}

it('Chromium: Alt-↓ and a direct front set slide the knob with the same magnet snap, slower than the drag follow (C06, C19)',async()=>{
    const scene={actions:null as Actions|null};
    const view=await render(<Scene onActions={a=>{scene.actions=a;}}/>);
    try{
        // The group mounts with its width spring running (it has no
        // `initial`): sample the knob once it rests at 800px, so the
        // track's own growth does not read as knob travel.
        await expect.poll(()=>group().style.willChange===''&&group().getBoundingClientRect().width===800,{timeout:3000}).toBe(true);
        const style=getComputedStyle(column());
        expect(style.transitionProperty).toBe('left');
        expect(style.transitionDuration).toBe('0.22s');
        expect(style.transitionTimingFunction).toBe('cubic-bezier(0.65, 0, 0.15, 1)');
        // The shortcut through the scene's own keyboard effect: the rotate
        // action, the same visibleTopIndices write a direct set makes.
        const ctx={isZoomedOut:false,sceneFocusArea:'main',selectedWindowId:'s',displaySelectedId:'s',mainStacks:STACKS,
            fan:null,visibleTopIndicesRef:{current:{s:0}},getSelectedFrontCell:()=>STACKS[0].cells[0],actions:scene.actions!} as unknown as Parameters<typeof executeSceneActions>[2];
        const key=await sampleSlide(()=>executeSceneActions(new KeyboardEvent('keydown',{cancelable:true}),SHORTCUT_IDS.STACK_ROTATE_NEXT,ctx));
        // A step to the next pip, moving right, with no overshoot on the way.
        expect(key.to).toBeGreaterThan(key.from);
        expect(Math.max(...key.progress.map(s=>s.p))).toBeLessThanOrEqual(1.001);
        // Slower than the old 140ms slide, done inside a few frames of 220ms.
        expect(key.arrived).toBeGreaterThan(180);
        expect(key.arrived).toBeLessThan(330);
        // The magnet: little travel in the first stretch, then a hard pull.
        const early=key.progress.filter(s=>s.t<=70);
        expect(early.length).toBeGreaterThan(0);
        expect(Math.max(...early.map(s=>s.p))).toBeLessThan(0.3);
        const late=key.progress.find(s=>s.t>=160)!;
        expect(late.p).toBeGreaterThan(0.8);
        // A direct front set (a drop, the knob's onSetTopSlot) back to the
        // first pip plays the same slide.
        const direct=await sampleSlide(()=>scene.actions!.winSetTopCell('s',0));
        expect(direct.to).toBeCloseTo(key.from,1);
        expect(direct.arrived).toBeGreaterThan(180);
        expect(direct.arrived).toBeLessThan(330);
        expect(Math.max(...direct.progress.filter(s=>s.t<=70).map(s=>s.p))).toBeLessThan(0.3);
        expect(Math.abs(direct.arrived-key.arrived)).toBeLessThan(50);
    }finally{await view.unmount();}
});
