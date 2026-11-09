import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import {BinaryConflictReview} from './BinaryConflictReview';
const h=vi.hoisted(()=>({mutate:vi.fn(),conflicts:[{conflict_id:'candidate',path:'image.bin',base_revision:1,current_revision:2,candidate_revision:3,candidate_hash:'candidatehash',writer_id:'other-window',edit_seq:4}]}));
vi.mock('@/hooks/useCellFileConflicts',()=>({useCellFileConflicts:()=>h.conflicts}));
vi.mock('@/lib/fileCommands',()=>({submitFileMutations:h.mutate}));
vi.mock('@/lib/fileBodies',()=>({pinnedFileUrl:(_channel:string,_cell:string,path:string,revision:number)=>`/files/${path}?revision=${revision}`}));
const file={name:'image.bin',language:'binary',encoding:'binary' as const,file_revision:2,body_hash:'serverhash',writer_id:'server',edit_seq:1};
beforeEach(()=>{vi.clearAllMocks();vi.stubGlobal('fetch',vi.fn(async()=>new Response(null,{headers:{'Content-Length':'4'}})));h.mutate.mockResolvedValue(undefined);});
describe('preserved binary version review',()=>{
 it('shows immutable provenance, byte count and revision-pinned downloads before conditional choice',async()=>{
  render(<BinaryConflictReview channel="channel" cellId="cell" file={file}/>);fireEvent.click(screen.getByRole('button',{name:'Review versions'}));
  expect(screen.getByText(/other-window.*revision 3/)).toBeVisible();await screen.findByText(/server.*4 bytes/);
  expect(screen.getAllByRole('link',{name:'Download version'})[1]).toHaveAttribute('href','/files/image.bin?revision=3');
  fireEvent.click(screen.getByRole('button',{name:'Use preserved version'}));
  await waitFor(()=>expect(h.mutate).toHaveBeenCalledWith('channel','cell',[expect.objectContaining({blob_hash:'candidatehash',base_revision:2,encoding:'binary'})],['candidate']));
 });
 it('disables a displayed resolution when a newer server revision arrives and preserves offline review',async()=>{
  const view=render(<BinaryConflictReview channel="channel" cellId="cell" file={file}/>);fireEvent.click(screen.getByRole('button',{name:'Review versions'}));
  view.rerender(<BinaryConflictReview channel="channel" cellId="cell" file={{...file,file_revision:4}}/>);
  expect(screen.getByRole('button',{name:'Use server version'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button',{name:'Review versions'}));h.mutate.mockRejectedValueOnce(new Error('offline'));
  fireEvent.click(screen.getByRole('button',{name:'Use preserved version'}));expect(await screen.findByRole('alert')).toHaveTextContent('offline');expect(screen.getByRole('dialog')).toBeVisible();
 });
});
