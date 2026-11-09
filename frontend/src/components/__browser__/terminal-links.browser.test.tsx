import {it,expect,vi} from 'vitest';

it.each(['terminal'])('actual %s link dispatcher hands a printed URL to api.openLink __F1807_F1828_F1847_D22',async kind=>{
 const response=await fetch(`/api/renderers/fixture-step6-${kind}/source`);
 expect(response.ok,await response.text()).toBe(true);
 const module=await import(/* @vite-ignore */ `/api/renderers/fixture-step6-${kind}/source`);
 const browser=vi.spyOn(window,'open').mockReturnValue(null);
 try{
  const openLink=vi.fn();
  const host=document.createElement('div');document.body.append(host);
  const api={getContent:async()=>JSON.stringify({terminal_id:crypto.randomUUID(),ws_path:'/__step6_no_live_pty',label:'Local link control'}),getFilename:()=>`sample.${kind}`,isReadOnly:()=>true,
   getFileUrl:(name:string)=>`${window.location.origin}/api/cellcap/test/files/${name}`,getSiblingFile:async()=>'',revealFile:vi.fn(),openLink,
   getSurface:async()=>null,onSurfaceChange:()=>()=>{},
   loadCss:()=>{},reportChange:()=>{},onFilesChange:()=>()=>{},onSetContent:()=>()=>{},onFlush:()=>()=>{},ready:()=>{}};
  const cleanup=await module.default.mount(host,api);
  try{
   await vi.waitFor(()=>expect(host.querySelector('button')).not.toBeNull());
   await vi.waitFor(()=>expect(host.querySelector('.xterm-screen')).not.toBeNull());
   const captured=(window as unknown as {__step6TerminalLink:(event:MouseEvent,href:string)=>void}).__step6TerminalLink;
   expect(captured).toBeTypeOf('function');
   captured(new MouseEvent('click'),'http://localhost:5173/');
   expect(openLink).toHaveBeenCalledExactlyOnceWith('http://localhost:5173/');
   expect(browser).not.toHaveBeenCalled();
  }finally{cleanup();host.remove();}
 }finally{browser.mockRestore();delete (window as unknown as {__step6TerminalLink?:unknown}).__step6TerminalLink;}
},30000);
