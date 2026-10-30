import {it,expect,vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {NativeRenderer} from '../NativeRenderer';

vi.mock('@/lib/api',async original=>({...await original<typeof import('@/lib/api')>(),getApiUrl:()=>`${window.location.origin}/api`}));

it('actual Monaco and SDK context retain an edit made after an older content commit',async()=>{
  const hostEdits=vi.fn(),replacements:string[]=[];
  let body='initial',notify:()=>void=()=>{},field:HTMLTextAreaElement|undefined;
  const edit=(next:string)=>{body=next;if(field)field.value=next;notify();};
  const fixtureWindow=window as typeof window & {
    monaco?:unknown;__monacoSchedule?:(content:string)=>void;
  };
  const priorMonaco=fixtureWindow.monaco;
  fixtureWindow.monaco={editor:{defineTheme:()=>{},setTheme:()=>{},remeasureFonts:()=>{},
    create:(container:HTMLElement,options:{value:string})=>{
      body=options.value;field=document.createElement('textarea');field.value=body;
      field.setAttribute('aria-label','Scheduled Monaco');container.appendChild(field);
      field.addEventListener('input',()=>edit(field!.value));
      const model={getValue:()=>body,getFullModelRange:()=>({}),pushEditOperations:(_selection:unknown,edits:{text:string}[])=>{
        replacements.push(edits[0].text);edit(edits[0].text);
      }};
      return {getValue:()=>body,getModel:()=>model,onDidChangeModelContent:(callback:(event:unknown)=>void)=>{notify=()=>callback({isFlush:false});},
        saveViewState:()=>({}),restoreViewState:()=>{},updateOptions:()=>{},dispose:()=>field?.remove()};
    },
  }};
  let interrupted=false;
  fixtureWindow.__monacoSchedule=content=>{
    if(content==='edit one'&&!interrupted){interrupted=true;edit('edit two');}
  };
  const props={rendererId:'fixture-monaco-schedule',rendererHash:'schedule',fileName:'schedule.txt',isActiveFocus:true,onContentChange:hostEdits};
  const view=await render(<NativeRenderer {...props} fileContent="initial"/>);
  try{
    await vi.waitFor(()=>expect(field).toBeDefined(),{timeout:30000});
    edit('edit one');
    await vi.waitFor(()=>expect(interrupted).toBe(true));
    await vi.waitFor(()=>expect(body).toBe('edit two'));
    expect(replacements).toEqual([]);
    expect(hostEdits.mock.calls.map(call=>call[0])).toEqual(['edit one','edit two']);
    edit(body+' + third');
    await vi.waitFor(()=>expect(hostEdits.mock.calls.at(-1)?.[0]).toBe('edit two + third'));
    expect(body).toBe('edit two + third');
    await view.rerender(<NativeRenderer {...props} fileContent="external replacement"/>);
    await vi.waitFor(()=>expect(body).toBe('external replacement'));
    expect(replacements).toEqual(['external replacement']);
    expect(hostEdits).toHaveBeenCalledTimes(3);
  }finally{
    await view.unmount();fixtureWindow.monaco=priorMonaco;delete fixtureWindow.__monacoSchedule;
  }
},45000);
