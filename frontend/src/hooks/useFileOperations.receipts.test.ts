// @vitest-environment jsdom
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {renderHook} from '@testing-library/react';
import {fileFixture} from '@/test/fileFixtures';
const controls=vi.hoisted(()=>({openAlert:vi.fn(async()=>{}),openConfirm:vi.fn(async()=>true)}));
vi.mock('@/stores/useCollimateStore',()=>({useCollimateStore:{getState:()=>controls}}));
import {useFileOperations} from './useFileOperations';
import {buildFileTree} from '@/lib/fileTree';
beforeEach(()=>vi.clearAllMocks());
describe('file operation receipts',()=>{
    it.each(['file','folder'] as const)('does not report %s creation before its receipt',async kind=>{
        let accept!:()=>void;
        const receipt=new Promise<void>(resolve=>{accept=resolve;});
        const update=vi.fn(()=>receipt);
        const {result}=renderHook(()=>useFileOperations({files:[],onUpdateFiles:update,readOnly:false}));
        let completed=false;
        const creating=(kind==='file'?result.current.handleCreateFileAtPath('note.txt'):result.current.handleCreateFolder('','docs')).then(value=>{completed=true;return value;});
        await Promise.resolve();expect(update).toHaveBeenCalledOnce();expect(completed).toBe(false);
        accept();expect(await creating).toEqual({ok:true,path:kind==='file'?'note.txt':'docs'});
    });
    it('returns the refused receipt as a failed create instead of opening a nonexistent file',async()=>{
        const {result}=renderHook(()=>useFileOperations({files:[],onUpdateFiles:async()=>{throw new Error('Receipt unavailable; operation retained');},readOnly:false}));
        expect(await result.current.handleCreateFileAtPath('note.txt')).toEqual({ok:false,error:'Receipt unavailable; operation retained'});
    });
    it('awaits deletion and displays a failed receipt',async()=>{
        const files=[fileFixture('note.txt','saved')];
        const {result}=renderHook(()=>useFileOperations({files,onUpdateFiles:async()=>{throw new Error('Server refused deletion');},readOnly:false}));
        await expect(result.current.handleDelete(buildFileTree(files)['note.txt'])).resolves.toBeUndefined();
        expect(controls.openAlert).toHaveBeenCalledWith({title:'Delete failed',body:'Server refused deletion'});
        expect(files.map(file=>file.name)).toEqual(['note.txt']);
    });
});
