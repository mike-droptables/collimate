import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AddFileDialog } from '@/components/AddFileDialog';
import { FileContextMenu } from '@/components/FileContextMenu';
import { TabContextMenu } from '@/components/cellEditor/TabContextMenu';

vi.mock('@/hooks/useRendererId', () => ({ useSupportedExtensions: () => [], useRendererId: () => ({renderer: null, error: null}) }));
vi.mock('@/components/OpenWithSubmenu', () => ({ hasOpenWithChoices: () => false, OpenWithSubmenu: () => null }));
vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));
afterEach(() => vi.restoreAllMocks());
it.each(['add', 'file', 'tab'] as const)('keeps %s popover in the viewport and cleans up outside/iframe dismissal __F0788', kind => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(100);
    const close = vi.fn();
    const point = { x: window.innerWidth + 50, y: window.innerHeight + 50 };
    const view = render(kind === 'add'
        ? <AddFileDialog {...point} basePath="" existingNames={[]} mode="file" onCancel={close} onCreate={vi.fn()} />
        : kind === 'file'
            ? <FileContextMenu {...point} target={{kind:'empty',basePath:''}} readOnly={false} clipboardLabel={null}
                onClose={close} onNewFile={vi.fn()} onNewFolder={vi.fn()} />
            : <TabContextMenu {...point} leafId="leaf" tab={{kind:'file',id:'tab',path:'note.txt'}}
                isPinned={false} pinnedPath={null} canPin={false} onClose={close} onLayoutChange={vi.fn()}
                onBeforeStructuralChange={vi.fn()} onCloseFileTab={vi.fn()} />);
    const anchor = kind === 'add' ? screen.getByPlaceholderText('filename') : screen.getByText(kind === 'file' ? 'New file' : 'Close');
    const panel = anchor.closest('.fixed') as HTMLElement;
    expect(panel.style.left).toBe(`${window.innerWidth - 208}px`);
    expect(panel.style.top).toBe(`${window.innerHeight - 108}px`);
    fireEvent.mouseDown(panel);
    expect(close).not.toHaveBeenCalled();
    fireEvent.mouseDown(document.body);
    expect(close).toHaveBeenCalledTimes(1);
    const frame = document.createElement('iframe');
    document.body.append(frame);
    frame.focus();
    fireEvent.blur(window);
    expect(close).toHaveBeenCalledTimes(2);
    view.unmount();
    fireEvent.mouseDown(document.body);
    fireEvent.blur(window);
    expect(close).toHaveBeenCalledTimes(2);
    frame.remove();
});
