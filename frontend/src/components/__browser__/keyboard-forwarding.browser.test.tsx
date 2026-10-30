import {it,expect} from 'vitest';
import {FIXTURE_BASIC_ID} from '@/test/rendererFixtures';
it('actual shell forwards physical Option chord and its modifier release __F0479_F0913',async()=>{
 const frame=document.createElement('iframe');const messages:Record<string,unknown>[]=[];
 const receive=(e:MessageEvent)=>{if(e.source===frame.contentWindow && e.data?.type?.startsWith('FORWARD_KEY'))messages.push(e.data);};
 window.addEventListener('message',receive);
 try {
  const loaded=new Promise<void>(resolve=>{frame.onload=()=>resolve();});frame.src=`/sandbox.html?renderer=${FIXTURE_BASIC_ID}`;document.body.append(frame);await loaded;
  frame.contentWindow!.postMessage({type:'SET_KEYBINDINGS',combos:[{key:'a',code:'KeyA',altKey:true,ctrlKey:false,shiftKey:false,metaKey:false}]},location.origin);
  // SET_KEYBINDINGS is asynchronous; a theme message provides a same-source
  // ordered acknowledgement using the production SET_THEME handler.
  frame.contentWindow!.postMessage({type:'SET_THEME',themeId:'keyboard-probe',scheme:'light',known:true,vars:{},fonts:{}},location.origin);
  await expect.poll(()=>frame.contentDocument!.documentElement.getAttribute('data-scheme')).toBe('light');
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keydown',{key:'å',code:'KeyA',altKey:true,cancelable:true}));
  await expect.poll(()=>messages.length).toBe(1);expect(messages[0]).toMatchObject({type:'FORWARD_KEYDOWN',key:'a',code:'KeyA',altKey:true});
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keyup',{key:'Alt',code:'AltLeft'}));
  await expect.poll(()=>messages.length).toBe(2);expect(messages[1]).toMatchObject({type:'FORWARD_KEYUP',key:'Alt'});
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keyup',{key:'Alt'}));await new Promise(resolve=>setTimeout(resolve,30));expect(messages.length).toBe(2);
 }finally{window.removeEventListener('message',receive);frame.remove();}
},15000);


it('overlapping forwarded chords retain each outstanding modifier release __F0913',async()=>{
 const frame=document.createElement('iframe');const messages:Record<string,unknown>[]=[];
 const receive=(e:MessageEvent)=>{if(e.source===frame.contentWindow && e.data?.type?.startsWith('FORWARD_KEY'))messages.push(e.data);};
 window.addEventListener('message',receive);
 try {
  const loaded=new Promise<void>(resolve=>{frame.onload=()=>resolve();});frame.src=`/sandbox.html?renderer=${FIXTURE_BASIC_ID}`;document.body.append(frame);await loaded;
  frame.contentWindow!.postMessage({type:'SET_KEYBINDINGS',combos:[
   {key:'ArrowRight',altKey:true},{key:'f',code:'KeyF',altKey:true,shiftKey:true},
  ]},location.origin);
  frame.contentWindow!.postMessage({type:'SET_THEME',themeId:'overlap-probe',scheme:'light',known:true,vars:{},fonts:{}},location.origin);
  await expect.poll(()=>frame.contentDocument!.documentElement.getAttribute('data-scheme')).toBe('light');
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',altKey:true}));
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keydown',{key:'F',code:'KeyF',altKey:true,shiftKey:true}));
  await expect.poll(()=>messages.length).toBe(2);
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keyup',{key:'Shift',altKey:true}));
  await expect.poll(()=>messages.length).toBe(3);
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keyup',{key:'Alt'}));
  await expect.poll(()=>messages.length).toBe(4);
  expect(messages.filter(m=>m.type==='FORWARD_KEYUP').map(m=>m.key)).toEqual(['Shift','Alt']);
 }finally{window.removeEventListener('message',receive);frame.remove();}
},15000);

it('the shell never forwards a bare "/" (the Commands pane key) while it forwards Mod+K (C19)',async()=>{
 const {buildForwardedCombos}=await import('@/lib/keybindings/forwardedCombos');
 const combos=buildForwardedCombos();
 expect(combos.some(c=>c.key==='/'&&!c.ctrlKey&&!c.metaKey&&!c.altKey)).toBe(false);
 expect(combos.some(c=>c.key==='k'&&(c.ctrlKey||c.metaKey))).toBe(true);
 const frame=document.createElement('iframe');const messages:Record<string,unknown>[]=[];
 const receive=(e:MessageEvent)=>{if(e.source===frame.contentWindow && e.data?.type?.startsWith('FORWARD_KEY'))messages.push(e.data);};
 window.addEventListener('message',receive);
 try {
  const loaded=new Promise<void>(resolve=>{frame.onload=()=>resolve();});frame.src=`/sandbox.html?renderer=${FIXTURE_BASIC_ID}`;document.body.append(frame);await loaded;
  frame.contentWindow!.postMessage({type:'SET_KEYBINDINGS',combos},location.origin);
  frame.contentWindow!.postMessage({type:'SET_THEME',themeId:'slash-probe',scheme:'light',known:true,vars:{},fonts:{}},location.origin);
  await expect.poll(()=>frame.contentDocument!.documentElement.getAttribute('data-scheme')).toBe('light');
  const mod=combos.find(c=>c.key==='k')!;
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keydown',{key:'/',code:'Slash',cancelable:true}));
  frame.contentWindow!.dispatchEvent(new KeyboardEvent('keydown',{key:'k',code:'KeyK',ctrlKey:mod.ctrlKey,metaKey:mod.metaKey,cancelable:true}));
  await expect.poll(()=>messages.length).toBe(1);
  expect(messages[0]).toMatchObject({type:'FORWARD_KEYDOWN',key:'k'});
 }finally{window.removeEventListener('message',receive);frame.remove();}
},15000);

it('the shell forwards Mod+P (Find file) from a renderer as it forwards Mod+K (P03)',async()=>{
 const {buildForwardedCombos}=await import('@/lib/keybindings/forwardedCombos');
 const combos=buildForwardedCombos();
 const mod=combos.find(c=>c.key==='p'&&(c.ctrlKey||c.metaKey))!;
 expect(mod).toBeDefined();
 const frame=document.createElement('iframe');const messages:Record<string,unknown>[]=[];
 const receive=(e:MessageEvent)=>{if(e.source===frame.contentWindow && e.data?.type?.startsWith('FORWARD_KEY'))messages.push(e.data);};
 window.addEventListener('message',receive);
 try {
  const loaded=new Promise<void>(resolve=>{frame.onload=()=>resolve();});frame.src=`/sandbox.html?renderer=${FIXTURE_BASIC_ID}`;document.body.append(frame);await loaded;
  frame.contentWindow!.postMessage({type:'SET_KEYBINDINGS',combos},location.origin);
  frame.contentWindow!.postMessage({type:'SET_THEME',themeId:'find-file-probe',scheme:'light',known:true,vars:{},fonts:{}},location.origin);
  await expect.poll(()=>frame.contentDocument!.documentElement.getAttribute('data-scheme')).toBe('light');
  const press=new KeyboardEvent('keydown',{key:'p',code:'KeyP',ctrlKey:mod.ctrlKey,metaKey:mod.metaKey,cancelable:true});
  frame.contentWindow!.dispatchEvent(press);
  await expect.poll(()=>messages.length).toBe(1);
  expect(messages[0]).toMatchObject({type:'FORWARD_KEYDOWN',key:'p',ctrlKey:mod.ctrlKey,metaKey:mod.metaKey});
  // Forwarded means the shell kept the key: no print dialog from inside a renderer.
  expect(press.defaultPrevented).toBe(true);
 }finally{window.removeEventListener('message',receive);frame.remove();}
},15000);
