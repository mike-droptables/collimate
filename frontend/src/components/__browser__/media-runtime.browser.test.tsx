import {it,expect,vi} from 'vitest';

it.each(['audio','video'])('actual %s renderer preserves controls, retry, URL replacement and layout __F1807_F1828_F1847',async kind=>{
 const module=await import(/* @vite-ignore */ `/api/renderers/fixture-step6-${kind}/source`);
 const host=document.createElement('div');host.style.cssText='width:700px;height:450px';document.body.append(host);
 const mount=host.attachShadow({mode:'open'});
 const bytes=new Uint8Array(8044);const view=new DataView(bytes.buffer);
 const word=(offset:number,text:string)=>[...text].forEach((c,i)=>bytes[offset+i]=c.charCodeAt(0));
 word(0,'RIFF');view.setUint32(4,8036,true);word(8,'WAVEfmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,8000,true);view.setUint32(28,8000,true);view.setUint16(32,1,true);view.setUint16(34,8,true);word(36,'data');view.setUint32(40,8000,true);bytes.fill(128,44);
 const first=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));const second=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));
 let url=first;let replace:(text:string)=>void=()=>{};
 const api={getContent:async()=>'',getFilename:()=>`clip.${kind==='audio'?'wav':'webm'}`,getFileUrl:()=>url,isReadOnly:()=>true,reportChange:()=>{},onFilesChange:()=>()=>{},onSetContent:(fn:(text:string)=>void)=>{replace=fn;return()=>{};},ready:()=>{}};
 const cleanup=await module.default.mount(mount,api);
 try{
  await vi.waitFor(()=>expect(mount.querySelector(kind)).not.toBeNull());
  const media=mount.querySelector<HTMLMediaElement>(kind)!;
  expect(media.controls).toBe(true);expect(media.src).toBe(first);
  if(kind==='video'){expect((media as HTMLVideoElement).playsInline).toBe(true);expect(media.style.objectFit).toBe('contain');}
  else expect(media.style.width).toBe('min(520px, 90%)');
  expect(media.parentElement!.style.padding).toBe(kind==='audio'?'24px':'16px');
  expect(mount.textContent).toContain(kind==='audio'?'WAV':'WEBM');
  media.dispatchEvent(new Event('error'));
  await vi.waitFor(()=>expect(mount.textContent).toContain('Could not play'));
  expect(mount.textContent).toContain(`unsupported format (.${kind==='audio'?'wav':'webm'})`);
  (mount.querySelector('button') as HTMLButtonElement).click();
  await vi.waitFor(()=>expect(mount.querySelector(kind)).not.toBeNull());
  expect(mount.querySelector(kind)).not.toBe(media);expect(mount.textContent).not.toContain('Could not play');
  mount.querySelector(kind)!.dispatchEvent(new Event('error'));
  await vi.waitFor(()=>expect(mount.textContent).toContain('Could not play'));
  url=second;replace('authoritative update');
  await vi.waitFor(()=>expect(mount.querySelector<HTMLMediaElement>(kind)?.src).toBe(second));
  expect(mount.textContent).not.toContain('Could not play');
 }finally{cleanup();host.remove();URL.revokeObjectURL(first);URL.revokeObjectURL(second);}
},30000);
