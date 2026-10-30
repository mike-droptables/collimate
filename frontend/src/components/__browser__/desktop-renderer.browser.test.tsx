import {it,expect,vi} from 'vitest';

it.each(['terminal'])('actual %s link dispatcher keeps desktop and browser routing __F1807_F1828_F1847_D22',async kind=>{
 const response=await fetch(`/api/renderers/fixture-step6-${kind}/source`);
 expect(response.ok,await response.text()).toBe(true);
 const module=await import(/* @vite-ignore */ `/api/renderers/fixture-step6-${kind}/source`);
 const browser=vi.spyOn(window,'open').mockReturnValue(null);
 try{
  for(const available of [true,false]){
   const desktop={openExternal:vi.fn(),capabilities:{}};const reveal=vi.fn();
   const host=document.createElement('div');document.body.append(host);
   const api={desktop:available?desktop:undefined,getContent:async()=>JSON.stringify({terminal_id:crypto.randomUUID(),ws_path:'/__step6_no_live_pty',label:'Local link control'}),getFilename:()=>`sample.${kind}`,isReadOnly:()=>true,
    getFileUrl:(name:string)=>`${window.location.origin}/api/cellcap/test/files/${name}`,getSiblingFile:async()=>'',revealFile:reveal,
    getSurface:async()=>null,onSurfaceChange:()=>()=>{},
    loadCss:()=>{},reportChange:()=>{},onFilesChange:()=>()=>{},onSetContent:()=>()=>{},onFlush:()=>()=>{},ready:()=>{}};
   const cleanup=await module.default.mount(host,api);
   try{
    await vi.waitFor(()=>expect(host.querySelector('button')).not.toBeNull());
    await vi.waitFor(()=>expect(host.querySelector('.xterm-screen')).not.toBeNull());
    const captured=(window as unknown as {__step6TerminalLink:(event:MouseEvent,href:string)=>void}).__step6TerminalLink;
    expect(captured).toBeTypeOf('function');
    captured(new MouseEvent('click'),'https://example.test/terminal');
    if(available)expect(desktop.openExternal).toHaveBeenCalledWith('https://example.test/terminal');
    else expect(browser).toHaveBeenCalledWith('https://example.test/terminal','_blank','noopener');
    if(available)expect(browser).not.toHaveBeenCalled();
   }finally{cleanup();host.remove();browser.mockClear();}
  }
 }finally{browser.mockRestore();delete (window as unknown as {__step6TerminalLink?:unknown}).__step6TerminalLink;}
},30000);
