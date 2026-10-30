import {afterEach,expect,it} from 'vitest';
import {AnnotationUnavailableError,requestSandboxAnnotation,type AnnotationRequest} from '@/lib/rendererAnnotation';

// The real sandbox.html answering annotation requests: the frame half of
// the wire (ANNOTATION_REQUEST/CANCEL in, ANNOTATION_ACK/RESULT/CAPABLE out)
// against the host half, requestSandboxAnnotation.

const closes:(()=>void)[]=[];
afterEach(()=>{for(const close of closes.splice(0))close();});

// Answers a selection at once; a pick per its hint: refuse, fail, or wait
// for the user (here: the abort).
const ANSWERING=`export default {mount:async(container,api)=>{window.__signals=[];
window.__off=api.onAnnotationRequest(async(request,signal)=>{window.__signals.push(signal);window.__last=request;
if(request.mode==='selection')return {label:'note.txt › line 1',text:'hello',anchor:{line:1}};
if(request.hint==='refuse'){const error=new Error('Nothing to pick here.');error.name='AnnotationUnavailable';throw error;}
if(request.hint==='fail')throw new Error('boom');
return new Promise(resolve=>signal.addEventListener('abort',()=>resolve(null)));});
container.textContent='mounted';api.ready();return()=>{};}};`;
const SILENT=`export default {mount:async(container,api)=>{container.textContent='mounted';api.ready();return()=>{};}};`;

type Child=Window&{__signals?:AbortSignal[];__off?:()=>void;__last?:AnnotationRequest};
function sandbox(code:string,canInteract=true){
    const iframe=document.createElement('iframe');const seen:{type:string;[key:string]:unknown}[]=[];
    const receive=(event:MessageEvent)=>{
        if(event.source!==iframe.contentWindow)return;
        const data=event.data;
        if(data?.type==='GET_DOCUMENT'){iframe.contentWindow!.postMessage({type:'PREVIEW_PRIMITIVE_RESULT',id:data.id,value:{content:'hello',editSeq:0,materializedSeq:0,readOnly:false,canInteract,files:[]}},location.origin);return;}
        if(data?.type==='RENDERER_BUNDLE_REQUEST'){iframe.contentWindow!.postMessage({type:'RENDERER_BUNDLE_RESULT',token:data.token,code},location.origin);return;}
        if(typeof data?.type==='string')seen.push(data);
    };
    window.addEventListener('message',receive);
    iframe.src='/sandbox.html?renderer=annotation-fixture&document=annotation-document&filename=note.txt';document.body.append(iframe);
    closes.push(()=>{window.removeEventListener('message',receive);iframe.remove();});
    const child=()=>iframe.contentWindow as Child;
    const ask=(request:AnnotationRequest,signal=new AbortController().signal)=>requestSandboxAnnotation(child(),request,signal,()=>true);
    const mounted=()=>expect.poll(()=>iframe.contentDocument?.getElementById('mount')?.textContent,{timeout:10000}).toBe('mounted');
    return {seen,child,ask,mounted,types:()=>seen.map(m=>m.type),post:(message:unknown)=>child().postMessage(message,location.origin)};
}
const refusal=(promise:Promise<unknown>)=>promise.then(()=>{throw new Error('resolved');},(error:unknown)=>error as AnnotationUnavailableError);

it('a renderer with a handler is announced capable, acknowledges, and its answer reaches the host',async()=>{
    const frame=sandbox(ANSWERING);await frame.mounted();
    await expect.poll(()=>frame.seen.find(m=>m.type==='ANNOTATION_CAPABLE')).toEqual({type:'ANNOTATION_CAPABLE',capable:true});
    await expect(frame.ask({id:'sel-1',mode:'selection'})).resolves.toEqual({label:'note.txt › line 1',text:'hello',anchor:{line:1}});
    expect(frame.seen.filter(m=>m.requestId==='sel-1').map(m=>m.type)).toEqual(['ANNOTATION_ACK','ANNOTATION_RESULT']);
    await frame.ask({id:'sel-2',mode:'selection',hint:'h'.repeat(300)});
    expect(frame.child().__last).toEqual({id:'sel-2',mode:'selection',hint:'h'.repeat(200)});
},20000);

it('a cancel aborts the handler\'s signal',async()=>{
    const frame=sandbox(ANSWERING);await frame.mounted();
    const controller=new AbortController();
    const pick=frame.ask({id:'pick-1',mode:'pick'},controller.signal);
    await expect.poll(()=>frame.child().__signals?.length).toBe(1);
    controller.abort('cancelled by the host');
    await expect(pick).rejects.toBe('cancelled by the host');
    await expect.poll(()=>frame.child().__signals![0].aborted).toBe(true);
    await expect.poll(()=>frame.seen.find(m=>m.requestId==='pick-1'&&m.type==='ANNOTATION_RESULT')).toEqual({type:'ANNOTATION_RESULT',requestId:'pick-1',cancelled:true});
},20000);

it('maps a renderer\'s refusal and failure, an unknown mode and a view that takes no annotations',async()=>{
    const frame=sandbox(ANSWERING);await frame.mounted();
    const refused=await refusal(frame.ask({id:'pick-2',mode:'pick',hint:'refuse'}));
    expect([refused.reason,refused.message]).toEqual(['renderer','Nothing to pick here.']);
    await expect(frame.ask({id:'pick-3',mode:'pick',hint:'fail'})).rejects.toThrow('boom');
    await expect(frame.ask({id:'odd',mode:'lasso' as never})).rejects.toThrow('Unknown annotation mode.');
    const locked=sandbox(ANSWERING,false);await locked.mounted();
    const read=await refusal(locked.ask({id:'pick-4',mode:'pick'}));
    expect([read.reason,read.message]).toEqual(['renderer','This view takes no annotations.']);
},20000);

it('a renderer without a handler answers unsupported at once',async()=>{
    const frame=sandbox(SILENT);await frame.mounted();
    const started=performance.now();
    const refused=await refusal(frame.ask({id:'none',mode:'pick'}));
    expect(refused.reason).toBe('not-capable');
    expect(performance.now()-started).toBeLessThan(1000);
    expect(frame.types()).not.toContain('ANNOTATION_ACK');
},20000);

it('PING_READY re-announces the capability and nothing of a request',async()=>{
    const frame=sandbox(ANSWERING);await frame.mounted();
    await expect.poll(()=>frame.types()).toContain('ANNOTATION_CAPABLE');
    frame.seen.length=0;
    frame.post({type:'PING_READY'});
    await expect.poll(()=>frame.seen.filter(m=>m.type==='ANNOTATION_CAPABLE')).toEqual([{type:'ANNOTATION_CAPABLE',capable:true}]);
    expect(frame.types().filter(type=>/^ANNOTATION_(REQUEST|ACK|RESULT)$/.test(type))).toEqual([]);
},20000);

it('withdrawing the handler announces it and answers the pending requests unsupported',async()=>{
    const frame=sandbox(ANSWERING);await frame.mounted();
    const pick=refusal(frame.ask({id:'pick-5',mode:'pick'}));
    await expect.poll(()=>frame.child().__signals?.length).toBe(1);
    frame.child().__off!();
    expect((await pick).reason).toBe('not-capable');
    expect(frame.child().__signals![0].aborted).toBe(true);
    await expect.poll(()=>frame.seen.filter(m=>m.type==='ANNOTATION_CAPABLE').at(-1)).toEqual({type:'ANNOTATION_CAPABLE',capable:false});
    expect((await refusal(frame.ask({id:'pick-6',mode:'pick'}))).reason).toBe('not-capable');
},20000);
