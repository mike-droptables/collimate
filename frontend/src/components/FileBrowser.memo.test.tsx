// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
import {cleanup,fireEvent,render} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
const treeRender=vi.hoisted(()=>vi.fn());
vi.mock('./FileTreeNode',()=>({FileTree:()=>{treeRender();return <div>tree rows</div>}}));
import {FileBrowser} from './FileBrowser';
afterEach(()=>{cleanup();vi.clearAllMocks()});
it('opening file chrome leaves the unchanged tree element alone __F0876_F0877',()=>{
 const files=Array.from({length:5000},(_,i)=>(fileFixture(`file-${i}.txt`, '', {language:'text', timestamp:''})));
 const view=render(<FileBrowser files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}/>);expect(treeRender).toHaveBeenCalledOnce();treeRender.mockClear();fireEvent.contextMenu(view.container.firstElementChild!);fireEvent.click(view.getByText('New file'));expect(treeRender).not.toHaveBeenCalled();expect(view.getByPlaceholderText('filename')).toBeDefined();
});
