// @vitest-environment jsdom
import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {FileOperation} from './fileProtocol';
import type {Cell,VirtualFile} from './types';
const network=vi.hoisted(()=>({mutate:vi.fn(),flush:vi.fn(async()=>{}),request:vi.fn(),owner:undefined as undefined|import('./fileDocument').FileDocument,save:vi.fn(async(_record:import('./fileDocumentStorage').PendingFileOperation)=>{}),remove:vi.fn(async()=>{}),pending:[] as import('./fileDocumentStorage').PendingFileOperation[],local:[] as import('./fileDocument').DocumentRecord[],draftSave:vi.fn(async(_record:import('./fileDocument').DocumentRecord)=>{}),draftRemove:vi.fn(async()=>{})}));
vi.mock('./fileDocuments',()=>({mutateFiles:network.mutate,flushFileDocuments:network.flush,observeCellDocuments:()=>{},hasDirtyDocuments:()=>false,getFileDocument:()=>network.owner,retargetFileDocument:async()=>false}));
vi.mock('./fileDocumentStorage',()=>({operationJournal:{save:network.save,remove:network.remove,load:async()=>network.pending},documentJournal:{load:async()=>network.local,save:network.draftSave,remove:network.draftRemove}}));
vi.mock('./api/client',async original=>({...await original<typeof import('./api/client')>(),getApiUrl:()=>'/api',requestJson:network.request}));
import {replaceCellFiles,submitFileMutations,applyMutationResult,recoverFileOperations,moveCellFiles} from './fileCommands';
import {useCollimateStore} from '@/stores/useCollimateStore';
const file=(revision=7):VirtualFile=>({name:'old.txt',file_revision:revision,body_hash:'text-hash',encoding:'utf-8',writer_id:'previous',edit_seq:1,language:'text'});
const seed=()=>{const cell:Cell={id:'cell',name:'Cell',files:[file()]};const channel={id:'channel',name:'Channel',nav_stacks:[],main_stacks:[{id:'stack',name:'',cells:[cell]}],nav_selection_id:null,main_selection_id:'stack'};useCollimateStore.setState({state:{active_channel_id:'channel',channels:[channel]},activeChannel:channel,cellCache:{cell}});};
beforeEach(()=>{
    vi.clearAllMocks();network.owner=undefined;network.pending=[];network.local=[];seed();network.request.mockResolvedValue({files:[]});
    network.mutate.mockImplementation(async(_channel,_cell,operation:FileOperation)=>({operation_id:operation.operation_id,outcome:'accepted',files:operation.mutations.map(mutation=>({path:mutation.path,file_revision:mutation.base_revision+1,body_hash:mutation.kind==='delete'?null:'new-hash',deleted:mutation.kind==='delete',writer_id:mutation.writer_id,edit_seq:mutation.edit_seq,language:mutation.language??'text',encoding:mutation.encoding??'utf-8'})),conflicts:[]}));
});
describe('file command transport',()=>{
    it.each(['id','missing-target','source-revision','target-revision','writer','sequence','source-tombstone','target-tombstone'])('retains a move with invalid %s acknowledgement and replays its exact group',async corruption=>{
        const reply=(move:NonNullable<import('./fileDocumentStorage').PendingFileOperation['move']>)=>({operation_id:move.operation_id,outcome:'accepted',files:[],conflicts:[],source_files:[{path:'old.txt',file_revision:8,body_hash:null,deleted:true,writer_id:move.writer_id,edit_seq:move.edit_seq,language:'text',encoding:'utf-8'}],target_files:[{path:'new.txt',file_revision:3,body_hash:'text-hash',deleted:false,writer_id:move.writer_id,edit_seq:move.edit_seq,language:'text',encoding:'utf-8'}]});
        network.request.mockImplementationOnce(async(_path,_method,move)=>{
            const result=reply(move);
            if(corruption==='id')result.operation_id='unrelated';
            if(corruption==='missing-target')result.target_files=[];
            if(corruption==='source-revision')result.source_files[0].file_revision=7;
            if(corruption==='target-revision')result.target_files[0].file_revision=2;
            if(corruption==='writer')result.target_files[0].writer_id='other';
            if(corruption==='sequence')result.source_files[0].edit_seq=99;
            if(corruption==='source-tombstone')result.source_files[0].deleted=false;
            if(corruption==='target-tombstone')result.target_files[0].deleted=true;
            return result;
        });
        await expect(moveCellFiles('channel','cell','target',[{source_path:'old.txt',target_path:'new.txt',source_revision:7,target_revision:2}])).rejects.toThrow('Invalid move acknowledgement');
        const pending=network.save.mock.calls[0][0];
        expect(network.remove).not.toHaveBeenCalled();expect(useCollimateStore.getState().cellCache.cell.files.map(file=>file.name)).toContain('old.txt');
        network.pending=[pending];network.request.mockImplementationOnce(async(_path,_method,move)=>reply(move));
        await recoverFileOperations('channel','cell');
        expect(network.request.mock.calls[1][2]).toBe(pending.move);
        expect(network.remove).toHaveBeenCalledWith(pending.key);
        expect(useCollimateStore.getState().cellCache.cell.files.map(file=>file.name)).not.toContain('old.txt');
    });
    it('replayed rename retargets a newer unmounted raw journal and releases its uncertainty hold',async()=>{
        const origin=location.origin,operation:FileOperation={operation_id:'rename-operation',mutations:[{path:'new.txt',kind:'put',writer_id:'command',edit_seq:1,base_revision:0,content_text:'captured body'},{path:'old.txt',kind:'delete',writer_id:'command',edit_seq:2,base_revision:7}],resolves:[]};
        network.pending=[{key:operation.operation_id,origin,channel:'channel',cellId:'cell',operation,renames:[{source:'old.txt',target:'new.txt',writerId:'window:old.txt',capturedEditSeq:1}]}];
        network.local=[{identity:{origin,channel:'channel',cellId:'cell',path:'old.txt'},writerId:'window:old.txt',body:'captured body',editSeq:2,localSeq:2,savedSeq:0,baseRevision:7,operation:null,conflicts:[],structuralUncertain:true,materializedSeq:1,recovery:{codecHash:'approved',snapshotId:'newer edit',payload:{text:'after rename'}}}];
        await recoverFileOperations('channel');
        expect(network.draftSave).toHaveBeenCalledWith(expect.objectContaining({identity:expect.objectContaining({path:'new.txt'}),writerId:'window:old.txt',editSeq:2,savedSeq:1,materializedSeq:1,structuralUncertain:false,recovery:{codecHash:'approved',snapshotId:'newer edit',payload:{text:'after rename'}}}));
        expect(network.draftSave.mock.invocationCallOrder[0]).toBeLessThan(network.draftRemove.mock.invocationCallOrder[0]);
        expect(network.draftRemove).toHaveBeenCalledWith('window:old.txt',expect.objectContaining({path:'old.txt'}));
        expect(network.remove).toHaveBeenCalledWith(operation.operation_id);
    });
    it('renames with one guarded atomic group and copies text CAS references without losing encoding',async()=>{
        await replaceCellFiles('channel','cell',[{...file(),name:'new.txt'}],undefined,[{source:'old.txt',target:'new.txt'}]);
        const operation=network.mutate.mock.calls[0][2];
        expect(operation.mutations).toEqual([expect.objectContaining({path:'new.txt',base_revision:0,kind:'put',blob_hash:'text-hash',encoding:'utf-8'}),expect.objectContaining({path:'old.txt',base_revision:7,kind:'delete'})]);
        expect(network.save.mock.invocationCallOrder[0]).toBeLessThan(network.mutate.mock.invocationCallOrder[0]);
        expect(useCollimateStore.getState().cellCache.cell.files.map(f=>f.name)).toEqual(['new.txt']);
    });
    it('uses a retained tombstone revision when recreating a path',async()=>{
        network.request.mockResolvedValue({files:[{path:'new.txt',file_revision:11,deleted:true}]});
        await replaceCellFiles('channel','cell',[file(),{...file(),name:'new.txt'}]);
        expect(network.mutate.mock.calls[0][2].mutations[0]).toMatchObject({path:'new.txt',base_revision:11});
    });
    it('retains exact uncertain operations and never publishes optimistic bytes on a refused save',async()=>{
        network.mutate.mockRejectedValue(new Error('disk refused'));
        const before=useCollimateStore.getState().cellCache.cell.files;
        await expect(submitFileMutations('channel','cell',[{path:'old.txt',kind:'put',writer_id:'writer',edit_seq:1,base_revision:7,content_text:'new text'}])).rejects.toThrow('disk refused');
        expect(network.save.mock.calls[0][0].operation).toEqual(network.mutate.mock.calls[0][2]);expect(network.remove).not.toHaveBeenCalled();
        expect(useCollimateStore.getState().cellCache.cell.files).toBe(before);
    });
    it('retries an uncertain creation by its original operation ID and scope',async()=>{
        network.mutate.mockRejectedValueOnce(new Error('lost receipt'));
        const mutations:FileOperation['mutations']=[{path:'new.txt',kind:'put',writer_id:'writer',edit_seq:1,base_revision:0,content_text:'created body'}];
        await expect(submitFileMutations('channel','cell',mutations)).rejects.toThrow('lost receipt');
        const retained=network.save.mock.calls[0][0];
        network.pending=[retained,{...retained,key:'different-cell',cellId:'other',operation:{...retained.operation,operation_id:'different-cell'}}];
        await recoverFileOperations('channel','cell');
        expect(network.mutate).toHaveBeenCalledTimes(2);
        expect(network.mutate.mock.calls[1][2]).toBe(retained.operation);
        expect(network.remove).toHaveBeenCalledWith(retained.key);
        expect(network.remove).not.toHaveBeenCalledWith('different-cell');
        expect(useCollimateStore.getState().cellCache.cell.files.map(file=>file.name)).toContain('new.txt');
    });
    it('does not let an older acknowledgement delete a newer accepted file',()=>{
        useCollimateStore.getState().patchCell('cell',{files:[file(10)]});
        applyMutationResult('cell',{operation_id:'old',outcome:'accepted',files:[{path:'old.txt',file_revision:8,body_hash:null,deleted:true,writer_id:'old',edit_seq:1,language:'text',encoding:'utf-8'}],conflicts:[]});
        expect(useCollimateStore.getState().cellCache.cell.files[0].file_revision).toBe(10);
    });
});
