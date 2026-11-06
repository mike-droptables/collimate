// @vitest-environment jsdom
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {expect,it,vi} from 'vitest';
import {FileTreeNode} from './FileTreeNode';
import {buildFileTree} from '@/lib/fileTree';
import {fileFixture} from '@/test/fileFixtures';
import {useCollimateStore} from '@/stores/useCollimateStore';

it('consumes rename Enter and reports a lost receipt without an uncaught rejection',async()=>{
    const alert=vi.fn(async()=>true);useCollimateStore.setState({openAlert:alert});
    const rename=vi.fn(async()=>{throw new Error('Lost receipt; exact operation retained');});
    render(<FileTreeNode node={buildFileTree([fileFixture('note.txt','saved')])['note.txt']} level={0} onOpenFile={()=>{}} onCreateFile={()=>{}} onDelete={()=>{}} onRename={rename} onDropFiles={()=>{}} readOnly={false}/>);
    fireEvent.click(screen.getByTitle('Rename'));
    const input=screen.getByRole('textbox');fireEvent.change(input,{target:{value:'new.txt'}});
    const event=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true});
    fireEvent(input,event);expect(event.defaultPrevented).toBe(true);
    await waitFor(()=>expect(alert).toHaveBeenCalledWith({title:'Rename failed',body:'Lost receipt; exact operation retained'}));
    expect(rename).toHaveBeenCalledOnce();
});
