import {fireEvent,render,screen} from '@testing-library/react';
import {describe,expect,it,vi} from 'vitest';
import {FileConflictNotice} from './FileConflictNotice';
vi.mock('@/hooks/useCellFileConflicts',()=>({useCellFileConflicts:()=>[{path:'closed.bin',conflict_id:'preserved'}]}));
vi.mock('@/lib/fileDocuments',()=>({subscribeDocuments:()=>()=>{},cellDocuments:()=>[{identity:{path:'dirty.txt'},getSnapshot:()=>({competing:{file_revision:2},conflicts:[]})}]}));
describe('file list conflict visibility',()=>{
 it('keeps server candidates and closed dirty documents visible and opens review by path',()=>{
  const open=vi.fn();render(<FileConflictNotice channel="channel" cellId="cell" onOpenFile={open}/>);
  expect(screen.getByRole('button',{name:'Conflict · closed.bin · Review versions'})).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'Conflict · dirty.txt · Review versions'}));expect(open).toHaveBeenCalledWith('dirty.txt');
 });
});
