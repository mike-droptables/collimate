// @vitest-environment jsdom
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {renderHook} from '@testing-library/react';
import {fileFixture} from '@/test/fileFixtures';
import type {UpdateFilesOptions} from '@/lib/fileTransfer';
import type {VirtualFile} from '@/lib/types';
const controls=vi.hoisted(()=>({openAlert:vi.fn(async()=>{}),openConfirm:vi.fn(async(_dialog:{body:string})=>true),openChoice:vi.fn(async()=>null),runs:{}}));
vi.mock('@/stores/useCollimateStore',()=>({useCollimateStore:{getState:()=>controls}}));
import {useFileOperations} from './useFileOperations';
import {buildFileTree} from '@/lib/fileTree';

// A folder delete or rename names its folder to the host, so the cell's
// repositories there go or re-key with the files; a file never does.
const files=()=>[fileFixture('app/a.txt','a'),fileFixture('app/lib/b.txt','b'),fileFixture('top.txt','t')];
function mount(){
    const calls:{next:VirtualFile[];options?:UpdateFilesOptions}[]=[];
    const current=files();
    const onUpdateFiles=vi.fn(async(update:(files:VirtualFile[])=>VirtualFile[],options?:UpdateFilesOptions)=>{calls.push({next:update(current),options});});
    const {result}=renderHook(()=>useFileOperations({files:current,onUpdateFiles,readOnly:false}));
    return {result,calls,tree:buildFileTree(current)};
}
beforeEach(()=>vi.clearAllMocks());
describe('folder operations',()=>{
    it('a folder delete sends folder_deletes with the files it removes',async()=>{
        const {result,calls,tree}=mount();
        await result.current.handleDelete(tree.app);
        expect(calls).toHaveLength(1);
        expect(calls[0].options).toEqual({folders:{folder_deletes:['app']}});
        expect(calls[0].next.map(f=>f.name)).toEqual(['top.txt']);
    });
    it('a file delete names no folder',async()=>{
        const {result,calls,tree}=mount();
        await result.current.handleDelete(tree['top.txt']);
        expect(calls[0].options).toBeUndefined();
    });
    it('a folder rename sends folder_moves beside its renames',async()=>{
        const {result,calls,tree}=mount();
        await result.current.handleRename(tree.app,'src');
        expect(calls[0].options?.folders).toEqual({folder_moves:[{source:'app',target:'src'}]});
        expect(calls[0].options?.renames).toEqual([{source:'app/a.txt',target:'src/a.txt'},{source:'app/lib/b.txt',target:'src/lib/b.txt'}]);
        const nested=buildFileTree(files()).app.children.lib;
        await result.current.handleRename(nested,'core');
        expect(calls[1].options?.folders).toEqual({folder_moves:[{source:'app/lib',target:'app/core'}]});
    });
    it('a file rename names no folder',async()=>{
        const {result,calls,tree}=mount();
        await result.current.handleRename(tree['top.txt'],'top.md');
        expect(calls[0].options?.folders).toBeUndefined();
    });
    it.each(['.git','.GIT','git~1','.git.'])('refuses %s as a file, folder or rename name',async name=>{
        const {result,calls,tree}=mount();
        expect(await result.current.handleCreateFileAtPath(`${name}/HEAD`)).toEqual({ok:false,error:'Reserved name'});
        expect(await result.current.handleCreateFolder('app',name)).toEqual({ok:false,error:'Reserved name'});
        await result.current.handleRename(tree.app,name);
        expect(controls.openAlert).toHaveBeenCalledWith(expect.objectContaining({body:'Reserved name'}));
        expect(calls).toEqual([]);
    });
});

// A folder delete names the history it takes along (§11), and a
// repository's file-less folder deletes by its folder operation alone.
describe('folder operations on repositories',()=>{
    const repo=(path:string)=>({path,branch:'main',upstream:null,ahead:null,behind:null,detached:false});
    function mountWith(repositories:ReturnType<typeof repo>[],cellId?:string){
        const calls:{next:VirtualFile[];options?:UpdateFilesOptions;current:VirtualFile[]}[]=[];
        const current=files();
        const onUpdateFiles=vi.fn(async(update:(files:VirtualFile[])=>VirtualFile[],options?:UpdateFilesOptions)=>{calls.push({next:update(current),options,current});});
        const {result}=renderHook(()=>useFileOperations({files:current,onUpdateFiles,readOnly:false,repositories,cellId}));
        return {result,calls,onUpdateFiles,tree:buildFileTree(current,repositories.map(r=>r.path).filter(Boolean))};
    }
    it('the delete dialog says the repositories’ git history leaves the cell',async()=>{
        const {result,tree}=mountWith([repo('app'),repo('app/lib'),repo('elsewhere')]);
        await result.current.handleDelete(tree.app);
        expect(controls.openConfirm).toHaveBeenCalledWith(expect.objectContaining({body:'Delete `app`? 2 files will be removed. The git history of the 2 repositories in it (their branches, stashes and reflogs) is removed from the cell.'}));
        await result.current.handleDelete(tree.app.children.lib);
        expect(controls.openConfirm).toHaveBeenLastCalledWith(expect.objectContaining({body:'Delete `app/lib`? 1 file will be removed. The git history of the repository at app/lib (its branches, stash and reflogs) is removed from the cell.'}));
    });
    it('a folder without a repository says nothing of git',async()=>{
        const {result,tree}=mountWith([repo('elsewhere')]);
        await result.current.handleDelete(tree.app);
        expect(controls.openConfirm.mock.calls[0][0].body).toBe('Delete `app`? 2 files will be removed.');
    });
    it('a repository with no file deletes by folder_deletes alone and is never renamed',async()=>{
        const {result,calls,tree}=mountWith([repo('vendor')]);
        await result.current.handleDelete(tree.vendor);
        expect(controls.openConfirm.mock.calls[0][0].body).toContain('Delete empty folder `vendor`? The git history of the repository at vendor');
        expect(calls[0].options).toEqual({folders:{folder_deletes:['vendor']}});
        expect(calls[0].next).toBe(calls[0].current);
        await result.current.handleRename(tree.vendor,'moved');
        expect(calls).toHaveLength(1);
    });
    it('a delete refused while another run holds the cell’s git offers Duplicate',async()=>{
        const {result,onUpdateFiles,tree}=mountWith([repo('app')],'c1');
        const message='git is in use by run abcd1234 on this cell; Duplicate the cell for a second working copy';
        onUpdateFiles.mockRejectedValueOnce(Object.assign(new Error(message),{response:{status:409,data:{detail:message}}}));
        await result.current.handleDelete(tree.app);
        expect(controls.openChoice).toHaveBeenCalledWith(expect.objectContaining({body:message,options:[{id:'duplicate',label:'Duplicate cell'}]}));
        expect(controls.openAlert).not.toHaveBeenCalled();
    });
});
