import React from 'react';
import {act,cleanup,fireEvent,render} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {StackGroup, type StackCallbacks} from './StackGroup';
import {FocusProvider} from '@/contexts/FocusContext';
import {DEFAULT_MIN_CELL_SIZE, useSpatialStore} from '@/stores/useSpatialStore';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {useChannelSettingsStore} from '@/stores/useChannelSettingsStore';
import {maxStackWidthFor} from '@/hooks/useStackLayout';
import type {Stack} from '@/lib/types';
import * as stackWidth from '@/lib/stackWidth';
import {putChannelSettings,type ChannelSettings} from '@/lib/api';
import {CENTER_CLICK_DELAY_MS} from '@/hooks/useCenterPanGesture';
// No network: the settings load never settles (the tests seed the doc),
// and saves resolve at once.
vi.mock('@/lib/api',async(importOriginal)=>({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  fetchChannelSettings:vi.fn(()=>new Promise(()=>{})),
  putChannelSettings:vi.fn(()=>Promise.resolve({})),
}));
const control=vi.hoisted(()=>({motion:null as null|((id:string,busy:boolean)=>void)}));
vi.mock('./StackItem',()=>({StackItem:(props:{onTransitionChange?: typeof control.motion})=>{control.motion=props.onTransitionChange??null;return <div/>;}}));
vi.mock('./capabilities/CapabilitiesManageCard',()=>({CapabilitiesManageCard:()=><div data-capabilities-manage/>}));
const two:Stack={id:'s',name:'Research',cells:[{id:'a',name:'a',files:[]},{id:'b',name:'b',files:[]}]};
const callbacks:StackCallbacks={onSelect(){},onClose(){},onRemoveCell(){},onUpdate(){},onSetTopSlot(){}};
const ui=(o:{top?:number;stack?:Stack;selected?:boolean;capabilities?:boolean}={})=><FocusProvider><StackGroup stack={{...(o.stack??two),stack_type:o.capabilities?'capabilities':undefined}} width={800} height={500} isSelected={o.selected??true} viewMode="stack" visibleTopIndex={o.top??0} callbacks={callbacks}/></FocusProvider>;
const head=()=>document.querySelector('[data-stack-head]') as HTMLElement;
const name=()=>document.querySelector('[data-stack-name]') as HTMLElement|null;
const pips=()=>Array.from(document.querySelectorAll<HTMLElement>('[data-scrub-pip]'));
const front=()=>document.querySelector('[data-scrub-knob-column] [data-scrub-pip]') as HTMLElement;
const count=()=>document.querySelector('[data-stack-count]') as HTMLElement|null;
const nameText=()=>name()?.textContent;
// The name fades to a transparent colour, never by opacity (an opacity fade
// runs on the compositor and turns the cards' text grayscale while it runs).
const hidden=(el:HTMLElement)=>el.className.split(/\s+/).includes('text-transparent');
afterEach(()=>{cleanup();vi.useRealTimers();vi.clearAllMocks();useSpatialStore.setState({settlingMoveSourceId:null});});

it('the head is 33px: the name on its top line, a quiet pip per cell beneath it (C06)',()=>{
  render(ui());
  expect(head().style.height).toBe('33px');
  expect(nameText()).toBe('Research');
  // A pip per cell (the front one under the knob) plus the knob's dot.
  expect(pips()).toHaveLength(3);
  expect(front().style.top).toBe('23px');
});

it('over the head the name fades and the knob rises; it comes back on leaving (C06)',()=>{
  render(ui());
  expect(hidden(name()!)).toBe(false);
  fireEvent.pointerEnter(head());
  expect(hidden(name()!)).toBe(true);
  expect(front().style.width).toBe('15px');
  fireEvent.pointerLeave(head());
  expect(hidden(name()!)).toBe(false);
  expect(front().style.width).toBe('6px');
});

it('the name and knob are brass on the selected stack, ink-2 and teal elsewhere; Capabilities keeps a teal name (C06) (C10)',()=>{
  render(ui());
  expect(name()!.className).toContain('text-brass');
  expect(front().style.background).toBe('var(--brass)');
  cleanup();
  render(ui({selected:false}));
  expect(name()!.className).toContain('text-ink-2');
  expect(front().style.background).toBe('var(--teal)');
  cleanup();
  render(ui({capabilities:true}));
  expect(name()!.className).toContain('text-teal');
  expect(front().style.background).toBe('var(--brass)');
});

it('a single cell has no pip, no knob and no count: at rest the name holds the line alone, over the head only the hairline rail comes up (polish)',()=>{
  const one:Stack={id:'s',name:'Solo',cells:[{id:'a',name:'a',files:[]}]};
  render(ui({stack:one}));
  const rail=()=>document.querySelector('[data-stack-single-rail] [data-scrub-rail]') as HTMLElement;
  expect(document.querySelector('[data-stack-scrubber], [data-scrub-knob], [data-scrub-knob-column]')).toBeNull();
  expect(pips()).toHaveLength(0);
  expect(count()).toBeNull();
  expect(nameText()).toBe('Solo');
  expect(hidden(name()!)).toBe(false);
  expect(rail().dataset.tone).toBe('hidden');
  fireEvent.pointerEnter(head());
  expect(hidden(name()!)).toBe(true);
  expect(rail().dataset.tone).toBe('quiet');
  expect(pips()).toHaveLength(0);
  expect(document.querySelector('[data-scrub-knob-column]')).toBeNull();
  // The rail still takes the pointer, so the head answers over it.
  expect((document.querySelector('[data-stack-single-rail]') as HTMLElement).style.pointerEvents).toBe('auto');
  fireEvent.pointerLeave(head());
  expect(hidden(name()!)).toBe(false);
  expect(rail().dataset.tone).toBe('hidden');
});

it('a loose cell (one unnamed cell): no name line and no pip; the same bare rail over its head (polish)',()=>{
  render(ui({stack:{id:'s',name:'',cells:[{id:'a',name:'a',files:[]}]},selected:false}));
  const rail=()=>document.querySelector('[data-stack-single-rail] [data-scrub-rail]') as HTMLElement;
  expect(name()).toBeNull();
  expect(pips()).toHaveLength(0);
  expect(count()).toBeNull();
  expect(rail().dataset.tone).toBe('hidden');
  fireEvent.pointerEnter(head());
  expect(rail().dataset.tone).toBe('quiet');
  expect(pips()).toHaveLength(0);
});

it('the count sits right of the last pip at rest, mono and quiet, and leaves the name line; it fades out over the head (C06, polish)',()=>{
  const three:Stack={id:'s',name:'Frame',cells:[{id:'a',name:'a',files:[]},{id:'b',name:'b',files:[]},{id:'c',name:'c',files:[]}]};
  render(ui({stack:three}));
  expect(nameText()).toBe('Frame');
  expect(name()!.querySelector('[data-stack-count]')).toBeNull();
  const c=count()!;
  expect(c.textContent).toBe('3');
  expect(c.parentElement!.hasAttribute('data-stack-scrubber')).toBe(true);
  // After the last slot, centred on the at-rest dots.
  const slots=document.querySelectorAll('[data-scrub-slot]');
  expect(c.compareDocumentPosition(slots[slots.length-1])&Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  expect(c.style.top).toBe('23px');
  // The last pip's right edge (half a 4px dot past its 15px slot's centre), then 6px.
  expect(c.style.left).toBe('calc(100% + 0.5px)');
  expect(c.style.color).toBe('var(--ink-3)');
  fireEvent.pointerEnter(head());
  expect(c.style.color).toBe('transparent');
  fireEvent.pointerLeave(head());
  expect(c.style.color).toBe('var(--ink-3)');
  cleanup();
  // An unnamed stack: no name line, the count after its pips all the same.
  render(ui({stack:{...three,name:''}}));
  expect(name()).toBeNull();
  expect(count()!.textContent).toBe('3');
});

it('each pip carries its cell\'s title (C06)',()=>{
  render(ui({stack:{...two,cells:[{id:'a',name:'First',files:[]},{id:'b',name:'Second',files:[]}]}}));
  expect(Array.from(document.querySelectorAll<HTMLElement>('[data-scrub-slot]')).map(s=>s.title)).toEqual(['First','Second']);
});

it('the head controls show on hover and always on the selected stack (C06) (C10)',()=>{
  render(ui({selected:false}));
  const more=document.querySelector('[aria-label="Stack menu"]') as HTMLElement;
  // Hidden is a transparent colour (focus in the head shows them too); the
  // fade is a colour fade, never opacity.
  expect(more.className).toContain('not-group-focus-within/head:text-transparent');
  expect(more.className).not.toMatch(/opacity/);
  fireEvent.pointerEnter(head());
  expect(more.className).not.toContain('text-transparent');
  cleanup();
  render(ui());
  expect((document.querySelector('[aria-label="Stack menu"]') as HTMLElement).className).not.toContain('text-transparent');
});

it('reveals a changed front through transition and a second after settlement __D19 (C06)',()=>{
  vi.useFakeTimers();const view=render(ui());
  try {
    view.rerender(ui({top:1}));
    expect(hidden(name()!)).toBe(true);
    expect(front().style.width).toBe('15px');
    // Drive the actual supplied callback; jsdom's Motion frame clock does
    // not finish the initial group animation under fake timers.
    act(()=>{control.motion?.('group',false);control.motion?.('b',true);});
    act(()=>vi.advanceTimersByTime(2500));
    expect(front().style.width).toBe('15px');
    act(()=>control.motion?.('b',false));
    act(()=>vi.advanceTimersByTime(999));expect(front().style.width).toBe('15px');
    act(()=>vi.advanceTimersByTime(1));expect(front().style.width).toBe('6px');
    expect(hidden(name()!)).toBe(false);
  }finally{view.unmount();}
});

it('a new front slides the knob to its pip with the magnet snap, whichever path set it (C06, C19)',()=>{
  // Alt-↑/↓ (winRotate) and a drop or the knob (winSetTopCell) both write
  // the scene's visibleTopIndices, which reaches the head as visibleTopIndex.
  const view=render(ui());
  const column=()=>document.querySelector('[data-scrub-knob-column]') as HTMLElement;
  expect(column().style.left).toBe('0%');
  view.rerender(ui({top:1}));
  expect(column().style.left).toBe('100%');
  expect(column().style.transition).toBe('left 220ms cubic-bezier(.65,0,.15,1)');
});

it("the head's empty space keeps the owner's gestures: a click recentres, a drag pans (C06)",()=>{
  vi.useFakeTimers();
  let moved=false;
  const gesture:StackCallbacks={...callbacks,onCenterStack:vi.fn(),onCenterPanStart:vi.fn(()=>{moved=false;}),onCenterPanMove:vi.fn(()=>{moved=true;}),onCenterPanEnd:vi.fn(()=>moved)};
  const widths=useSpatialStore.getState().stackWidths;
  render(<FocusProvider><StackGroup stack={two} width={800} height={500} isSelected viewMode="stack" callbacks={gesture}/></FocusProvider>);
  const h=head();
  h.setPointerCapture=vi.fn();h.hasPointerCapture=()=>true;h.releasePointerCapture=vi.fn();
  const pointer=(type:string,x=100)=>fireEvent(h,new MouseEvent(type,{bubbles:true,cancelable:true,button:0,buttons:type==='pointerup'?0:1,clientX:x}));
  pointer('pointerdown');pointer('pointerup');fireEvent.click(h,{detail:1});
  act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS));
  expect(gesture.onCenterStack).toHaveBeenCalledTimes(1);
  pointer('pointerdown');pointer('pointermove',160);pointer('pointerup',160);fireEvent.click(h,{detail:1});
  act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS));
  expect(gesture.onCenterPanMove).toHaveBeenCalledTimes(1);
  expect(gesture.onCenterStack).toHaveBeenCalledTimes(1);
  // A double-click whose second press travelled was a pan: no width change.
  fireEvent.doubleClick(h,{clientX:160});
  expect(useSpatialStore.getState().stackWidths).toBe(widths);
  // The arrows and the menu button never start the pan.
  pointer('pointerdown');pointer('pointerup');
  const before=(gesture.onCenterPanStart as ReturnType<typeof vi.fn>).mock.calls.length;
  fireEvent(document.querySelector('[aria-label="Stack menu"]')!,new MouseEvent('pointerdown',{bubbles:true,cancelable:true,button:0}));
  expect((gesture.onCenterPanStart as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
});

it('Capabilities names in teal, selected or not (D-C05) (C05)',()=>{
  for (const selected of [true,false]) {
    render(ui({capabilities:true,selected}));
    expect(name()!.className).toContain('text-teal');
    cleanup();
  }
});

describe('Capabilities pages through its manage card and then its cells',()=>{
  const slotTitles=()=>Array.from(document.querySelectorAll<HTMLElement>('[data-scrub-slot]')).map(el=>el.title);
  it('has a pip per cell plus one for the manage card, the first titled Manage, and counts every slot',()=>{
    render(ui({capabilities:true}));
    expect(document.querySelectorAll('[data-scrub-slot]')).toHaveLength(3);
    expect(slotTitles()[0]).toBe('Manage');
    expect(slotTitles().slice(1)).toEqual(['a','b']);
    expect(count()!.textContent).toBe('3');
  });
  it('slot 0 shows the manage card in the card position; slot k+1 shows cell k',()=>{
    render(ui({capabilities:true}));
    expect(document.querySelector('[data-manage-slot] [data-capabilities-manage]')).not.toBeNull();
    expect(document.querySelector('[data-scrub-slot="0"]')!.hasAttribute('data-current')).toBe(true);
    cleanup();
    render(ui({capabilities:true,top:2}));
    expect(document.querySelector('[data-manage-slot]')).toBeNull();
    expect(document.querySelector('[data-scrub-slot="2"]')!.hasAttribute('data-current')).toBe(true);
  });
  it('a single-cell standard stack keeps no pip, but Capabilities with one card has two',()=>{
    const one:Stack={id:'s',name:'Capabilities',cells:[{id:'a',name:'a',files:[]}]};
    render(ui({stack:one,capabilities:true}));
    expect(document.querySelectorAll('[data-scrub-slot]')).toHaveLength(2);
    expect(document.querySelector('[data-stack-single-rail]')).toBeNull();
  });
});

describe('a click on the head waits out the double-click window before it recentres (polish)',()=>{
  let moved=false;
  const gesture=()=>({...callbacks,onCenterStack:vi.fn(),onCenterPanStart:vi.fn(()=>{moved=false;}),onCenterPanMove:vi.fn(()=>{moved=true;}),onCenterPanEnd:vi.fn(()=>moved)}) satisfies StackCallbacks;
  const mount=(cb:StackCallbacks)=>{
    render(<FocusProvider><StackGroup stack={two} width={800} height={500} isSelected viewMode="stack" callbacks={cb}/></FocusProvider>);
    const h=head();
    h.setPointerCapture=vi.fn();h.hasPointerCapture=()=>true;h.releasePointerCapture=vi.fn();
    return h;
  };
  const pointer=(h:Element,type:string,x=100)=>fireEvent(h,new MouseEvent(type,{bubbles:true,cancelable:true,button:0,buttons:type==='pointerup'?0:1,clientX:x}));
  const click=(h:Element,detail:number,x=100)=>{pointer(h,'pointerdown',x);pointer(h,'pointerup',x);fireEvent.click(h,{detail,clientX:x});};
  beforeEach(()=>{
    vi.useFakeTimers();
    useSpatialStore.setState({minCellSize:DEFAULT_MIN_CELL_SIZE,stackWidths:{},maxColumns:{},allStacksByChannel:{},isMaxWidth:false});
    useCollimateStore.setState({activeChannel:{id:'11111111-1111-4111-8111-111111111111',name:'chan'}} as never);
    useChannelSettingsStore.setState({doc:{version:1,kind:'channel-settings',layout:{defaultStackWidth:null}} as unknown as ChannelSettings,loadedFor:'11111111-1111-4111-8111-111111111111'});
  });
  it('a single click recentres after the window, not before (polish)',()=>{
    const cb=gesture();const h=mount(cb);
    click(h,1);
    act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS-1));
    expect(cb.onCenterStack).not.toHaveBeenCalled();
    act(()=>vi.advanceTimersByTime(1));
    expect(cb.onCenterStack).toHaveBeenCalledTimes(1);
  });
  it('a double-click within the window recentres nothing and maximises (polish)',()=>{
    const cb=gesture();const h=mount(cb);
    useSpatialStore.setState({minCellSize:420});
    click(h,1);
    act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS/2));
    click(h,2);fireEvent.doubleClick(h,{clientX:100});
    act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS*4));
    expect(cb.onCenterStack).not.toHaveBeenCalled();
    expect(useSpatialStore.getState()).toMatchObject({minCellSize:maxStackWidthFor(window.innerWidth),isMaxWidth:true});
  });
  it('a click then a drag recentres nothing (polish)',()=>{
    const cb=gesture();const h=mount(cb);
    click(h,1);
    act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS/2));
    pointer(h,'pointerdown');pointer(h,'pointermove',160);pointer(h,'pointerup',160);fireEvent.click(h,{detail:1,clientX:160});
    act(()=>vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS*4));
    expect(cb.onCenterPanMove).toHaveBeenCalled();
    expect(cb.onCenterStack).not.toHaveBeenCalled();
  });
});

describe('the head\'s double-click is the max toggle Alt+F and the Width tool use (C06, polish)',()=>{
  const max=()=>maxStackWidthFor(window.innerWidth);
  const channelDefault=()=>useChannelSettingsStore.getState().doc?.layout?.defaultStackWidth;
  const loadSettings=(defaultStackWidth:number|null)=>useChannelSettingsStore.setState({doc:{version:1,kind:'channel-settings',layout:{defaultStackWidth}} as unknown as ChannelSettings,loadedFor:'11111111-1111-4111-8111-111111111111'});
  beforeEach(()=>{
    useSpatialStore.setState({minCellSize:DEFAULT_MIN_CELL_SIZE,stackWidths:{},maxColumns:{},allStacksByChannel:{},isMaxWidth:false});
    useCollimateStore.setState({activeChannel:{id:'11111111-1111-4111-8111-111111111111',name:'chan'}} as never);
    loadSettings(null);
  });
  const dbl=(el:Element,x=100)=>{
    fireEvent(el,new MouseEvent('pointerdown',{bubbles:true,cancelable:true,button:0,clientX:x}));
    fireEvent.doubleClick(el,{clientX:x});
  };
  it('shared ("all stacks" on): maximising from 700 stores 700 as the channel default, and the second double-click returns to it (polish)',()=>{
    loadSettings(640);
    useSpatialStore.setState({minCellSize:700,stackWidths:{other:420}});
    render(ui());
    dbl(head());
    expect(useSpatialStore.getState()).toMatchObject({minCellSize:max(),isMaxWidth:true,stackWidths:{}});
    expect(channelDefault()).toBe(700);
    expect(putChannelSettings).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111',{layout:{defaultStackWidth:700}});
    dbl(head());
    expect(useSpatialStore.getState()).toMatchObject({minCellSize:700,isMaxWidth:false});
  });
  it('goes through the one max operator Alt+F and the Width tool use (polish)',()=>{
    const toggle=vi.spyOn(stackWidth,'toggleMaxWidth');
    render(ui());
    dbl(head());
    expect(toggle).toHaveBeenCalledWith('s','11111111-1111-4111-8111-111111111111');
    toggle.mockRestore();
  });
  it('per stack ("all stacks" off), from the rail: this stack alone, its width before stored as the channel default and come back to (polish)',()=>{
    useSpatialStore.setState({allStacksByChannel:{['11111111-1111-4111-8111-111111111111']:false},stackWidths:{s:620,other:420}});
    render(ui());
    fireEvent.pointerEnter(head());
    const track=document.querySelector('[data-stack-scrubber]') as HTMLElement;
    dbl(track);
    expect(useSpatialStore.getState().stackWidths).toEqual({s:max(),other:420});
    expect(useSpatialStore.getState().maxColumns).toEqual({s:true});
    expect(channelDefault()).toBe(620);
    expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE);
    dbl(track);
    expect(useSpatialStore.getState().stackWidths).toEqual({s:620,other:420});
    expect(useSpatialStore.getState().maxColumns).toEqual({});
  });
  it('an already-full stack stores nothing and goes to the channel default (polish)',()=>{
    loadSettings(640);
    vi.mocked(putChannelSettings).mockClear();
    useSpatialStore.setState({minCellSize:max()});
    render(ui());
    dbl(head());
    expect(useSpatialStore.getState().minCellSize).toBe(640);
    cleanup();
    useSpatialStore.setState({allStacksByChannel:{['11111111-1111-4111-8111-111111111111']:false},stackWidths:{s:max()},maxColumns:{}});
    render(ui());
    dbl(head());
    expect(useSpatialStore.getState().stackWidths.s).toBe(640);
    expect(putChannelSettings).not.toHaveBeenCalled();
  });
  it('a single-cell head toggles the same way, through its bare rail (polish)',()=>{
    useSpatialStore.setState({minCellSize:600});
    render(ui({stack:{id:'s',name:'Solo',cells:[{id:'a',name:'a',files:[]}]}}));
    fireEvent.pointerEnter(head());
    dbl(document.querySelector('[data-stack-single-rail]')!);
    expect(useSpatialStore.getState().minCellSize).toBe(max());
    dbl(document.querySelector('[data-stack-single-rail]')!);
    expect(useSpatialStore.getState().minCellSize).toBe(600);
  });
  it('the arrows and the menu button keep their own double-clicks',()=>{
    render(ui());
    const before=useSpatialStore.getState().minCellSize;
    for (const label of ['Move stack left','Move stack right','Stack menu']) dbl(document.querySelector(`[aria-label="${label}"]`)!);
    expect(useSpatialStore.getState().minCellSize).toBe(before);
    expect(useSpatialStore.getState().stackWidths).toEqual({});
  });
  it('the head\'s title names the gesture',()=>{
    render(ui());
    expect(head().title).toContain('double-click for full width');
  });
});
