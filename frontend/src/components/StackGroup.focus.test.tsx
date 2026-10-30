import React, {Profiler} from 'react';
import {act, render} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import {StackGroup} from './StackGroup';
import {FocusProvider, useFocus, useFocusActions} from '@/contexts/FocusContext';
import type {FocusActions, FocusContextType} from '@/contexts/FocusContext';
vi.mock('./StackItem', () => ({StackItem: () => <div/>}));
it('keeps StackGroup out of editor focus-mode updates __F0565_F0564_F0559 (C06)', async () => {
  let actions!: FocusActions;
  let focus!: FocusContextType;
  let updates = 0;
  function Probe() {actions = useFocusActions(); focus = useFocus(); return null;}
  const stack = {id:'s',name:'s',cells:[{id:'c',name:'c',files:[]}]};
  const callbacks = {onSelect(){},onClose(){},onRemoveCell(){},onUpdate(){}};
  const view = render(<FocusProvider><Probe/><Profiler id="group" onRender={(_id,phase)=>{if(phase==='update')updates++;}}><StackGroup stack={stack} width={800} height={500} isSelected viewMode="stack" callbacks={callbacks}/></Profiler></FocusProvider>);
  try {
    // Exclude the initial Motion subscription/animation-start commit.
    await act(async()=>{await new Promise(resolve=>setTimeout(resolve,60));});
    updates=0;
    act(() => actions.setMainFocusMode('files'));
    act(() => actions.setMainFocusMode('file-editor'));
    expect(updates).toBe(0);
    expect(Object.keys(focus).sort()).toEqual(['focusArea','mainFocusMode','selectedWindowId','setFocusArea','setMainFocusMode','setSelectedWindowId'].sort());
  } finally {view.unmount();}
});
