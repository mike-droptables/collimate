import {afterEach,expect,it} from 'vitest';
import {handleSandboxHostMessage} from '@/lib/sandboxBridge';

// A pooled sandbox frame outlives its host: parking unmounts the
// SandboxIframe that answered it while the page keeps booting. A one-shot
// preview request the renderer sends meanwhile reaches no current host (the
// pool has no subscriber, and the bridge drops anything once its owner has
// let go), so the next host must still answer it when it attaches and pings.

const closes:(()=>void)[]=[];
afterEach(()=>{for(const close of closes.splice(0))close();});
// Boots like the milkdown renderer: some local work (here a gate the test
// opens), then the codec registration, then the editor and ready().
const code=`export default {mount:async(container,api)=>{await new Promise(go=>{window.__boot=go;});await api.registerSnapshotCodec({id:'fixture'});container.textContent='mounted';api.ready();return()=>{};}};`;
type Codec=()=>Promise<void>;
function pooledFrame(){
    const iframe=document.createElement('iframe');const requests:string[]=[];
    let host:((event:MessageEvent)=>void)|null=null;
    const receive=(event:MessageEvent)=>{
        if(event.source!==iframe.contentWindow)return;
        if(event.data?.type==='RENDERER_BUNDLE_REQUEST'){iframe.contentWindow!.postMessage({type:'RENDERER_BUNDLE_RESULT',token:event.data.token,code},location.origin);return;}
        requests.push(event.data?.type);host?.(event);
    };
    window.addEventListener('message',receive);
    iframe.src='/sandbox.html?renderer=reattach-fixture&document=reattach-document&filename=note.md';document.body.append(iframe);
    closes.push(()=>{window.removeEventListener('message',receive);iframe.remove();});
    /** What SandboxIframe does: subscribe through the bridge, and on acquire
     *  ping the frame. The returned detach is its unmount. */
    const attach=(codec:Codec)=>{
        let alive=true;const target=iframe.contentWindow!;
        host=event=>handleSandboxHostMessage(event,{target,isCurrent:()=>alive,ready:()=>{},rendererId:'reattach-fixture',fileName:'note.md',
            getDocument:()=>({content:'original',diffContent:null}),registerSnapshotCodec:codec});
        target.postMessage({type:'PING_READY'},location.origin);
        return ()=>{alive=false;host=null;};
    };
    const child=()=>iframe.contentWindow as Window&{__boot?:()=>void};
    return {requests,attach,child,mountText:()=>iframe.contentDocument?.getElementById('mount')?.textContent??''};
}
const registered:Codec=()=>Promise.resolve();

it('a request sent while the frame is parked is answered by the host that re-attaches',async()=>{
    const frame=pooledFrame();const detach=frame.attach(registered);
    await expect.poll(()=>typeof frame.child().__boot,{timeout:10000}).toBe('function');
    detach();
    frame.child().__boot!();
    await expect.poll(()=>frame.requests.includes('REGISTER_SNAPSHOT_CODEC')).toBe(true);
    frame.attach(registered);
    await expect.poll(frame.mountText,{timeout:5000}).toBe('mounted');
},20000);

it('an answer the parked host never sent is answered by the host that re-attaches',async()=>{
    const frame=pooledFrame();let settle=()=>{};
    const detach=frame.attach(()=>new Promise<void>(resolve=>{settle=resolve;}));
    await expect.poll(()=>typeof frame.child().__boot,{timeout:10000}).toBe('function');
    frame.child().__boot!();
    await expect.poll(()=>frame.requests.includes('REGISTER_SNAPSHOT_CODEC')).toBe(true);
    detach();settle();
    frame.attach(registered);
    await expect.poll(frame.mountText,{timeout:5000}).toBe('mounted');
},20000);
