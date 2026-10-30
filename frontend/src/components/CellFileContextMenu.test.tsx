import { fileFixture } from '@/test/fileFixtures';
import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {CellFileContextMenu} from './CellFileContextMenu';
import type {FileMenuTarget} from './FileContextMenu';
import type {TreeNode} from '@/lib/fileTree';
vi.mock('@/lib/guestOcclusion',()=>({useGuestOccluder:vi.fn()}));
vi.mock('@/components/OpenWithSubmenu',()=>({OpenWithSubmenu:()=>null,hasOpenWithChoices:()=>false}));
vi.mock('@/components/CompareWithSubmenu',()=>({CompareWithSubmenu:()=>null}));
afterEach(cleanup);
const node=(path:string,type:'file'|'folder'):TreeNode=>({path,name:path.split('/').pop()!,type,children:{},...(type==='file'?{fileData:fileFixture(path, 'bytes', {language:'text', timestamp:'now'})}:{})});
it.each([['folder',{kind:'folder',node:node('docs','folder')},'docs'],['empty',{kind:'empty',basePath:'docs'},'docs']] as const)('shares the actual %s paste destination and locked refusal__F0867',(_kind,target,destination)=>{
 const paste=vi.fn();const props={menu:{x:0,y:0,target:target as FileMenuTarget},readOnly:false,locked:false,transfer:{clipboardLabel:null,copyNode:vi.fn(),cutNode:vi.fn()},pasteHere:paste,cellId:'cell',onClose:vi.fn(),onNewFile:vi.fn(),onNewFolder:vi.fn(),onDelete:vi.fn(),onDuplicate:vi.fn()};
 const view=render(<CellFileContextMenu {...props}/>);fireEvent.click(screen.getByRole('button',{name:'Paste here'}));expect(paste).toHaveBeenCalledExactlyOnceWith(destination);
 view.rerender(<CellFileContextMenu {...props} menu={{x:0,y:0,target:{kind:'file',node:node('_runs/live/out.txt','file')}}} locked/>);
 expect(screen.queryByRole('button',{name:'Cut'})).toBeNull();expect(screen.queryByRole('button',{name:'Delete'})).toBeNull();fireEvent.click(screen.getByRole('button',{name:'Copy'}));expect(props.transfer.copyNode).toHaveBeenCalled();
});
