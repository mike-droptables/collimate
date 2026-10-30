import {afterEach,beforeEach,expect,it,vi} from 'vitest';
const forget=vi.hoisted(()=>vi.fn());
vi.mock('./fileDocuments',()=>({forgetCellDocumentView:forget,getFileDocument:()=>undefined,editCellDocument:vi.fn(),snapshotCellDocument:vi.fn()}));
vi.mock('./rendererBundle',()=>({answerRendererBundleRequest:vi.fn()}));
vi.mock('@/stores/useCollimateStore',()=>({useCollimateStore:{getState:()=>({cellCache:{},locateCell:()=>null})}}));
import {disposeSandboxView,handleSandboxHostMessage,type SandboxHostContext} from './sandboxBridge';
function fixture(){
    const target={postMessage:vi.fn()} as unknown as Window;
    const context:SandboxHostContext={target,isCurrent:()=>true,ready:vi.fn(),rendererId:'text',fileName:'note.txt',channel:'channel',cellId:'cell',onContentChange:vi.fn(),onSnapshotChange:vi.fn(),getDocument:()=>({content:'original',diffContent:null})};
    const deliver=(data:unknown)=>handleSandboxHostMessage({data,source:target,origin:window.location.origin} as MessageEvent,context);
    return {target,context,deliver};
}
beforeEach(()=>{vi.stubGlobal('window',{location:{origin:'http://localhost'}});vi.clearAllMocks();});
afterEach(()=>vi.unstubAllGlobals());
it('requires registered physical provenance and preserves it until physical disposal',()=>{
    const {target,context,deliver}=fixture();
    deliver({type:'FILE_UPDATED',viewId:'physical-one',viewSeq:1,content:'unknown'});expect(context.onContentChange).not.toHaveBeenCalled();
    deliver({type:'GET_DOCUMENT',viewId:'physical-one',id:1});
    deliver({type:'FILE_UPDATED',viewId:'physical-one',viewSeq:1,content:'one'});
    expect(context.onContentChange).toHaveBeenCalledWith('one',{viewId:'physical-one',viewSeq:1});
    deliver({type:'FILE_UPDATED',viewId:'attacker',viewSeq:2,content:'other'});expect(context.onContentChange).toHaveBeenCalledTimes(1);
    disposeSandboxView(target);expect(forget).toHaveBeenCalledWith('channel','cell','note.txt','physical-one');
    deliver({type:'FILE_UPDATED',viewId:'physical-one',viewSeq:2,content:'late'});expect(context.onContentChange).toHaveBeenCalledTimes(1);
});
it('rejects identity replacement and refused input without acknowledging another source',()=>{
    const {target,context,deliver}=fixture();deliver({type:'GET_DOCUMENT',viewId:'one',id:1});
    deliver({type:'GET_DOCUMENT',viewId:'two',id:2});
    expect(target.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({id:2,error:expect.stringContaining('physical disposal')}),window.location.origin);
    context.readOnly=true;deliver({type:'FILE_UPDATED',viewId:'one',viewSeq:1,content:'blocked'});
    deliver({type:'FILE_SNAPSHOT_UPDATED',viewId:'one',viewSeq:2,snapshot:{snapshotId:'blocked'}});
    expect(context.onContentChange).not.toHaveBeenCalled();expect(context.onSnapshotChange).not.toHaveBeenCalled();disposeSandboxView(target);
});
