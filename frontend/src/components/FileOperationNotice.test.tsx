// @vitest-environment jsdom
import {act,fireEvent,render,screen} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
const recovery=vi.hoisted(()=>({records:[] as import('@/lib/fileDocumentStorage').PendingFileOperation[],listener:()=>{},retry:vi.fn()}));
vi.mock('@/lib/fileDocumentStorage',()=>({operationJournal:{load:async()=>recovery.records,subscribe:(callback:()=>void)=>{recovery.listener=callback;return()=>{};}}}));
vi.mock('@/lib/fileCommands',()=>({recoverFileOperations:recovery.retry}));
vi.mock('@/lib/api/client',()=>({getApiUrl:()=>'/api'}));
import {FileOperationNotice} from './FileOperationNotice';
beforeEach(()=>{vi.clearAllMocks();recovery.records=[{key:'exact',origin:location.origin,channel:'channel',cellId:'cell',operation:{operation_id:'exact',mutations:[],resolves:[]}}];});
it('keeps a failed exact operation visible until retry succeeds',async()=>{
    recovery.retry.mockRejectedValueOnce(new Error('disk unavailable')).mockImplementationOnce(async()=>{recovery.records=[];recovery.listener();});
    render(<FileOperationNotice channel="channel" cellId="cell"/>);
    fireEvent.click(await screen.findByRole('button',{name:'Retry pending operation'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('disk unavailable');
    expect(recovery.retry).toHaveBeenCalledWith('channel','cell');
    await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Retry pending operation'}));});
    expect(screen.queryByRole('status')).toBeNull();
});
