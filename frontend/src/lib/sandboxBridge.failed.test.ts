import {afterEach,beforeEach,expect,it,vi} from 'vitest';
vi.mock('./fileDocuments',()=>({forgetCellDocumentView:vi.fn(),getFileDocument:()=>undefined,editCellDocument:vi.fn(),snapshotCellDocument:vi.fn()}));
vi.mock('./rendererBundle',()=>({answerRendererBundleRequest:vi.fn()}));
vi.mock('@/stores/useCollimateStore',()=>({useCollimateStore:{getState:()=>({cellCache:{},locateCell:()=>null})}}));
import {handleSandboxHostMessage,type SandboxHostContext} from './sandboxBridge';
function fixture(extra:Partial<SandboxHostContext>={}){
    const target={postMessage:vi.fn()} as unknown as Window;
    const context:SandboxHostContext={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'rich-notes',fileName:'note.md',...extra};
    const deliver=(data:unknown)=>handleSandboxHostMessage({data,source:target,origin:window.location.origin} as MessageEvent,context);
    return {context,deliver};
}
beforeEach(()=>{vi.stubGlobal('window',{location:{origin:'http://localhost'}});vi.clearAllMocks();});
afterEach(()=>vi.unstubAllGlobals());
it('RENDERER_FAILED reports the message to onFailed and does not imply ready',()=>{
    const onFailed=vi.fn(),{context,deliver}=fixture({onFailed});
    deliver({type:'RENDERER_FAILED',message:'Cannot read properties of undefined'});
    expect(onFailed).toHaveBeenCalledWith('Cannot read properties of undefined');expect(context.ready).not.toHaveBeenCalled();
});
it('RENDERER_FAILED without a message reports an empty one',()=>{
    const onFailed=vi.fn(),{deliver}=fixture({onFailed});
    deliver({type:'RENDERER_FAILED'});
    expect(onFailed).toHaveBeenCalledWith('');
});
it('RENDERER_FAILED is ignored by a host without onFailed, and from a superseded frame',()=>{
    const {context,deliver}=fixture();
    expect(()=>deliver({type:'RENDERER_FAILED',message:'x'})).not.toThrow();expect(context.ready).not.toHaveBeenCalled();
    const onFailed=vi.fn(),stale=fixture({onFailed,isCurrent:()=>false});
    stale.deliver({type:'RENDERER_FAILED',message:'late'});expect(onFailed).not.toHaveBeenCalled();
});
