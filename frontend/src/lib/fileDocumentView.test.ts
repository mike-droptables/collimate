import {expect,it,vi} from 'vitest';
import {FileDocument,type DocumentJournal} from './fileDocument';
const identity={origin:'http://test',channel:'channel',cellId:'cell',path:'note.txt'};
const descriptor={path:'note.txt',file_revision:1,body_hash:'base',deleted:false,writer_id:'server',edit_seq:1,language:'text',encoding:'utf-8' as const};
async function document(){
    const journal:DocumentJournal={load:vi.fn(async()=>[]),save:vi.fn(async()=>undefined),remove:vi.fn(async()=>undefined)};
    const owner=new FileDocument(identity,'writer',journal,vi.fn(),async()=> 'original',100000);
    await owner.initialize(descriptor);return {owner,journal};
}
it('captures immutable cumulative view acknowledgements with their exact body',async()=>{
    const {owner}=await document();
    owner.edit('first',false,{viewId:'one',viewSeq:1});const first=owner.getDocumentSnapshot();
    owner.edit('second',false,{viewId:'two',viewSeq:1});const second=owner.getDocumentSnapshot();
    owner.edit('third',false,{viewId:'one',viewSeq:2});
    expect(first).toMatchObject({content:'first',viewAcks:{one:1}});
    expect(second).toMatchObject({content:'second',viewAcks:{one:1,two:1}});
    expect(owner.getDocumentSnapshot()).toMatchObject({content:'third',viewAcks:{one:2,two:1}});
});
it('refuses duplicate and older view edits before touching the body',async()=>{
    const {owner}=await document();owner.edit('latest',false,{viewId:'one',viewSeq:3});
    owner.edit('stale duplicate',false,{viewId:'one',viewSeq:3});owner.edit('stale older',false,{viewId:'one',viewSeq:2});
    owner.editSnapshot({codecHash:'approved',snapshotId:'stale',payload:'{}'},{viewId:'one',viewSeq:1});
    expect(owner.getSnapshot()).toMatchObject({body:'latest',editSeq:1,viewAcks:{one:3}});
});
it('acknowledges an admitted equal body without claiming a new durable edit',async()=>{
    const {owner,journal}=await document();owner.edit('original',false,{viewId:'one',viewSeq:1});
    expect(owner.getSnapshot()).toMatchObject({editSeq:0,savedSeq:0,status:'Saved',viewAcks:{one:1}});
    expect(journal.save).not.toHaveBeenCalled();
});
it('does not acknowledge edits refused by a structural barrier; physical disposal forgets only that view',async()=>{
    const {owner}=await document();owner.edit('first',false,{viewId:'one',viewSeq:1});await owner.locallyDurable();
    owner.edit('first',false,{viewId:'two',viewSeq:1});await owner.beginStructural();
    owner.edit('blocked',false,{viewId:'one',viewSeq:2});
    expect(owner.getDocumentSnapshot()).toMatchObject({content:'first',viewAcks:{one:1,two:1}});
    owner.forgetView('one');expect(owner.getDocumentSnapshot().viewAcks).toEqual({two:1});
});

it('keeps an owner identity through rename but allocates a different identity for a recreated source',async()=>{
    const {owner}=await document(),before=owner.instanceId;
    await owner.moveTo({...descriptor,path:'renamed.txt',file_revision:2},0);
    const {owner:recreated}=await document();
    expect(owner.getDocumentSnapshot().ownerId).toBe(before);
    expect(recreated.instanceId).not.toBe(before);
});
