import { fileFixture } from '@/test/fileFixtures';
import {afterEach,expect,it,vi} from 'vitest';
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {FolderView} from './FolderView';
import {useCollimateStore} from '@/stores/useCollimateStore';
const files=[fileFixture('a.txt', 'a', {language:'text', timestamp:'now'}),fileFixture('dir/b.txt', 'b', {language:'text', timestamp:'now'})];
afterEach(()=>{cleanup();vi.restoreAllMocks();});
it('Backspace at the root never asks to delete the selected file__F0844',()=>{
 const confirm=vi.spyOn(useCollimateStore.getState(),'openConfirm').mockResolvedValue(false);
 const {container}=render(<FolderView files={files} cellId="keys" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}/>);
 fireEvent.click(screen.getByText('a.txt'));fireEvent.keyDown(container.firstChild!,{key:'Backspace'});expect(confirm).not.toHaveBeenCalled();
 fireEvent.keyDown(container.firstChild!,{key:'Delete'});expect(confirm).toHaveBeenCalledTimes(1);
});
it.each(['ctrlKey','metaKey'])('modified Backspace deletes in a subfolder without navigating up (%s)__F0844',(modifier)=>{
 const confirm=vi.spyOn(useCollimateStore.getState(),'openConfirm').mockResolvedValue(false);const folder=vi.fn();
 const {container}=render(<FolderView files={files} initialFolder="dir" cellId="keys" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} onCurrentFolderChange={folder}/>);
 fireEvent.click(screen.getByText('b.txt'));fireEvent.keyDown(container.firstChild!,{key:'Backspace',[modifier]:true});expect(confirm).toHaveBeenCalledTimes(1);expect(folder).toHaveBeenLastCalledWith('dir');
});
it('a right-click in a rename field keeps the browser menu: neither the tile\'s nor the folder\'s menu opens',()=>{
 const {container}=render(<FolderView files={files} cellId="keys" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}/>);
 fireEvent.click(screen.getByText('a.txt'));fireEvent.keyDown(container.firstChild!,{key:'F2'});
 const field=container.querySelector('input')!;expect(field).not.toBeNull();
 expect(fireEvent.contextMenu(field)).toBe(true);
 expect(screen.queryByText('Delete')).toBeNull();expect(screen.queryByText('New file')).toBeNull();
 // The folder's background still opens its own menu.
 expect(fireEvent.contextMenu(container.firstChild!)).toBe(false);
});
/** A synthetic paste event carrying `text` (the hook reads focus, not the target). */
function pasteText(text:string):boolean{
 const event=new Event('paste',{bubbles:true,cancelable:true});
 Object.defineProperty(event,'clipboardData',{value:{files:[],items:[],getData:(f:string)=>(f==='text/plain'?text:'')}});
 act(()=>{document.body.dispatchEvent(event);});
 return event.defaultPrevented;
}
it('Mod+V is left to the browser; its paste event lands in the folder on show (FP-05)',()=>{
 const {container}=render(<FolderView files={files} initialFolder="dir" cellId="keys" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}/>);
 const tile=screen.getByText('b.txt').closest('[role="button"]') as HTMLElement;
 act(()=>tile.focus());
 expect(fireEvent.keyDown(container.firstChild!,{key:'v',ctrlKey:true})).toBe(true);
 expect(fireEvent.keyDown(tile,{key:'v',metaKey:true})).toBe(true);
 expect(pasteText('# Heading')).toBe(true);
 expect(screen.getByText(/Paste text in/)).toBeDefined();
 expect(screen.getByText('dir',{selector:'span.font-mono'})).toBeDefined();
});
it('a paste with focus outside the folder view is not its paste (FP-05)',()=>{
 render(<FolderView files={files} cellId="keys" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}/>);
 expect(pasteText('words')).toBe(false);
 expect(screen.queryByText(/Paste text in/)).toBeNull();
});
