import {afterEach,expect,it} from 'vitest';
const closes:(()=>void)[]=[];
afterEach(()=>{for(const close of closes.splice(0))close();});
const code=`export default {mount:async(container,api)=>{window.__ackApi=api;window.__ackChanges=[];container.textContent=await api.getContent();api.onSetContent(body=>{window.__ackChanges.push(body);container.textContent=body;});api.ready();return()=>{};}};`;
function sandbox(){
    const iframe=document.createElement('iframe');const edits:{content?:string;snapshot?:unknown;viewId:string;viewSeq:number}[]=[];let viewId='';
    const receive=(event:MessageEvent)=>{
        if(event.source!==iframe.contentWindow)return;
        if(event.data?.type==='PREVIEW_PRIMITIVE_RESULT')return;
        if(event.data?.type==='GET_DOCUMENT'){
            viewId=event.data.viewId;
            iframe.contentWindow!.postMessage({type:'PREVIEW_PRIMITIVE_RESULT',id:event.data.id,value:{content:'original',editSeq:0,materializedSeq:0,readOnly:false,canInteract:true,files:[]}},location.origin);
        }
        if(event.data?.type==='GET_DOCUMENT_CONTENT')iframe.contentWindow!.postMessage({type:'PREVIEW_PRIMITIVE_RESULT',id:event.data.id,value:'original'},location.origin);
        if(event.data?.type==='RENDERER_BUNDLE_REQUEST')iframe.contentWindow!.postMessage({type:'RENDERER_BUNDLE_RESULT',token:event.data.token,code},location.origin);
        if(event.data?.type==='FILE_UPDATED'||event.data?.type==='FILE_SNAPSHOT_UPDATED')edits.push(event.data);
    };
    window.addEventListener('message',receive);iframe.src='/sandbox.html?renderer=ack-fixture&document=logical-document&filename=note.txt';document.body.append(iframe);
    closes.push(()=>{window.removeEventListener('message',receive);iframe.remove();});
    return {iframe,edits,viewId:()=>viewId,push:(content:string,covered:number)=>iframe.contentWindow!.postMessage({type:'SET_DOCUMENT',content,editSeq:covered,materializedSeq:covered,viewAcks:{[viewId]:covered}},location.origin)};
}
it('ignores delayed own echoes and accepts a cumulative covered external replacement',async()=>{
    const frame=sandbox();await expect.poll(()=>frame.iframe.contentDocument?.getElementById('mount')?.textContent,{timeout:10000}).toBe('original');
    const child=frame.iframe.contentWindow as Window&{__ackApi:{reportChange:(body:string)=>void};__ackChanges:string[]};
    child.__ackApi.reportChange('one');child.__ackApi.reportChange('two');
    await expect.poll(()=>frame.edits.length).toBe(2);expect(frame.edits.map(e=>e.viewSeq)).toEqual([1,2]);expect(frame.edits.every(e=>e.viewId===frame.viewId())).toBe(true);
    frame.push('one',1);frame.push('external covering both',2);
    await expect.poll(()=>child.__ackChanges).toEqual(['external covering both']);
    expect(frame.iframe.contentDocument?.getElementById('mount')?.textContent).toBe('external covering both');
},15000);
it('raw snapshot emissions share the same physical view highwater',async()=>{
    const frame=sandbox();await expect.poll(()=>frame.iframe.contentDocument?.getElementById('mount')?.textContent,{timeout:10000}).toBe('original');
    const child=frame.iframe.contentWindow as Window&{__ackApi:{reportChange:(body:string)=>void;saveSnapshot:(snapshot:unknown)=>void};__ackChanges:string[]};
    child.__ackApi.reportChange('one');child.__ackApi.saveSnapshot({codecHash:'approved',snapshotId:'raw-two',payload:'{}'});
    await expect.poll(()=>frame.edits.length).toBe(2);expect(frame.edits.map(e=>e.viewSeq)).toEqual([1,2]);
    frame.push('stale string',1);frame.push('materialized external replacement',2);
    await expect.poll(()=>child.__ackChanges).toEqual(['materialized external replacement']);
},15000);
