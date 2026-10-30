import { fixtureText } from '@/test/fileFixtures';
// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
import {cleanup, render, fireEvent} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import type {Cell, VirtualFile} from '@/lib/types';
const renderFile=vi.hoisted(()=>vi.fn());
vi.mock('@/components/FullFileEditor',()=>({FullFileEditor: (props: {file:VirtualFile})=>{renderFile(props.file.name);return <div>{fixtureText(props.file)}</div>}}));
import {TabBody, type TabBodyProps} from './TabBody';
import {useState} from 'react';
import {FilesPane} from './FilesPane';
import {createDefaultLayout, setFilesPane} from '@/lib/cellLayout';
afterEach(()=>{cleanup();vi.clearAllMocks()});
it('a one-file update leaves the other visited tabs mounted without renderer work __F0802',()=>{
    const files=Array.from({length:3},(_,i)=>(fileFixture(`file-${i}.txt`, 'original', {language:'text', timestamp:''})));
    const cell={id:'memo-cell',files} as Cell;
    const common={cell,readOnly:false,isActiveFocus:false,visible:false,editorShortcutsEnabled:false,isZoomTransitioning:false,registerEditorHandle:vi.fn(),onCloseFileTab:vi.fn(),updateFileContent:vi.fn(),onUpdateFiles:vi.fn(),onCloseTabById:vi.fn()};
    const props=files.map((file,i)=>({...common,tab:{id:`tab-${i}`,kind:'file',path:file.name}} as TabBodyProps));
    const view=(cell:Cell)=>props.map(p=><TabBody key={p.tab.id} {...p} cell={cell}/>);
    const mounted=render(view(cell));expect(renderFile).toHaveBeenCalledTimes(3);renderFile.mockClear();
    mounted.rerender(view({...cell,files:files.map((file,i)=>i===0?{...file,content:'edited'}:file)}));
    expect(renderFile.mock.calls.map(([name])=>name)).toEqual(['file-0.txt']);
    expect(mounted.getAllByText('original')).toHaveLength(2);
    expect(mounted.getByText('edited')).toBeDefined();
});

it('tree selection writes remain published without repainting visited heavy tabs__F0892', () => {
    const files = ['docs/a.txt', 'docs/b.txt', 'docs/c.txt'].map(name => (fileFixture(name, 'retained', {language: 'text', timestamp: ''})));
    const cell = { id: 'navigation-cell', files } as Cell;
    const initial = setFilesPane(createDefaultLayout({ filePaths: new Set(files.map(f => f.name)), pinnedPath: null }), { collapsed: false, viewMode: 'tree' });
    const noop = () => {};
    const common: Omit<TabBodyProps, 'tab'> = { cell, readOnly: false, isActiveFocus: false, visible: false, editorShortcutsEnabled: false,
        isZoomTransitioning: false, registerEditorHandle: noop, onCloseFileTab: noop,
        onUpdateFiles: noop, onCloseTabById: noop };
    const tabs = files.map((f, i) => ({ id: `heavy-${i}`, kind: 'file' as const, path: f.name }));
    const writes: unknown[] = [];
    function Harness() {
        const [layout, setLayout] = useState(initial);
        return <><FilesPane cell={cell} layout={layout} onLayoutChange={update => setLayout(old => {
            const next = update(old); if (next !== old) writes.push(next.filesPane.selection); return next;
        })} onOpenFile={noop} onUpdateFiles={noop} readOnly={false} activePath={null} />
            {tabs.map(tab => <TabBody key={tab.id} {...common} tab={tab} />)}
            <output data-testid="published-selection">{layout.filesPane.selection?.nodePath}</output></>;
    }
    const view = render(<Harness />);
    expect(renderFile).toHaveBeenCalledTimes(3);
    renderFile.mockClear(); writes.length = 0;
    const row = (path: string) => view.container.querySelector(`[data-filenode-path="${path}"]`)!;
    for (let i = 0; i < 20; i++) fireEvent.focus(row(files[i % 3].name));
    expect(writes).toHaveLength(20);
    expect(view.getByTestId('published-selection')).toHaveTextContent(files[19 % 3].name);
    fireEvent.focus(row(files[19 % 3].name));
    expect(writes).toHaveLength(20);
    fireEvent.click(row('docs'));
    expect(view.getByTestId('published-selection')).toHaveTextContent('docs');
    expect(renderFile).not.toHaveBeenCalled();
});
