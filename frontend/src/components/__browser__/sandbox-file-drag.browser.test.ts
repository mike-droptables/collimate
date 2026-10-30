import {it,expect} from 'vitest';
import {FIXTURE_BASIC_ID} from '@/test/rendererFixtures';
// Files dragged in from the desktop that enter the window over a renderer
// frame land in the frame's document, where the host's window listeners
// never see them. The real shell tells the host (OS_FILE_DRAG; sandboxBridge
// raises osFileDragActive), so the renderers go pointer-passive and the
// drag's next move reaches the canvas.
it('the shell reports a desktop file drag to the host, never its own drag or one without files',async()=>{
 const frame=document.createElement('iframe');const messages:unknown[]=[];
 const receive=(e:MessageEvent)=>{if(e.source===frame.contentWindow && e.data?.type==='OS_FILE_DRAG')messages.push(e.data);};
 window.addEventListener('message',receive);
 try {
  const loaded=new Promise<void>(resolve=>{frame.onload=()=>resolve();});frame.src=`/sandbox.html?renderer=${FIXTURE_BASIC_ID}`;document.body.append(frame);await loaded;
  const win=frame.contentWindow! as Window & typeof globalThis;
  const fire=(type:string,files:boolean)=>{
   const dataTransfer=new win.DataTransfer();
   if(files)dataTransfer.items.add(new win.File(['x'],'x.txt',{type:'text/plain'}));else dataTransfer.setData('text/plain','x');
   win.document.body.dispatchEvent(new win.DragEvent(type,{bubbles:true,cancelable:true,dataTransfer}));
  };
  // No files: not a desktop file drag.
  fire('dragenter',false);fire('dragover',false);
  // A drag the frame's own document started (a renderer's image drag).
  win.document.body.dispatchEvent(new win.DragEvent('dragstart',{bubbles:true}));
  fire('dragover',true);
  win.document.body.dispatchEvent(new win.DragEvent('dragend',{bubbles:true}));
  // A desktop file drag: its enter and every dragover report it. Messages
  // from one source arrive in order, so any report above would come first.
  fire('dragenter',true);
  await expect.poll(()=>messages.length).toBeGreaterThan(0);
  await new Promise(resolve=>setTimeout(resolve,50));
  expect(messages).toEqual([{type:'OS_FILE_DRAG'}]);
  fire('dragover',true);
  await expect.poll(()=>messages.length).toBe(2);
 }finally{window.removeEventListener('message',receive);frame.remove();}
},15000);
