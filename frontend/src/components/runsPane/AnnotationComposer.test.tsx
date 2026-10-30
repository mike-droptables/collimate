// The status pane's composer around its editor (DraftEditor, faked here):
// Send, Queued | Interrupt (with the offer to interrupt the queue), the queue
// bar and the place above or below it, Add annotation and its waiting row,
// context chips, expand, the edit and reply banners, the notices, and the
// layered Esc.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { forwardRef, useImperativeHandle } from 'react';

const editor = vi.hoisted(() => ({ text: vi.fn(async () => ''), focus: vi.fn(() => true), reset: vi.fn(), mounts: 0 }));
vi.mock('./DraftEditor', () => ({
    DraftEditor: forwardRef(function FakeDraftEditor(props: { initialText: string; onText: (t: string) => void }, ref) {
        useImperativeHandle(ref, () => ({ text: editor.text, focus: editor.focus, reset: editor.reset }));
        return <textarea data-testid="fake-editor" defaultValue={props.initialText} onChange={e => props.onText(e.target.value)} />;
    }),
}));
// The element holding the keyboard (a pooled frame's slot, say): inside a
// composer when that composer's root contains it.
const keys = vi.hoisted(() => ({ holder: null as Element | null }));
vi.mock('@/lib/selectionFocus', async (importOriginal) => ({
    ...(await importOriginal<object>()),
    keysInside: (within: unknown) => !!keys.holder && within instanceof Element && within.contains(keys.holder),
}));
const pick = vi.hoisted(() => ({ pending: false }));
vi.mock('@/lib/rendererAnnotation', async (importOriginal) => ({ ...(await importOriginal<object>()), hasPendingAnnotationPick: () => pick.pending }));

import { AnnotationComposer, type AnnotationComposerProps } from './AnnotationComposer';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, ctx, msg, seedPaneStore } from '@/test/annotationFixtures';

const KEY = `${CELL_ID}:run:run-1`;
const store = () => useCollimateStore.getState();
const draft = () => store().composerDrafts[KEY];
const mount = (props: Partial<AnnotationComposerProps> = {}) => {
    const onEscape = vi.fn();
    const view = render(<AnnotationComposer cellId={CELL_ID} channel="ch" scope={{ kind: 'run', runId: 'run-1' }} contextId="run-1"
        context={ctx('run-1', [msg('q1', { position: 1 }), msg('q2', { position: 2 }), msg('i1', { position: 0, mode: 'interrupt' })])}
        barOn={false} stopping={false} maximized={false} onEscape={onEscape} {...props} />);
    return { onEscape, ...view };
};
const mode = (m: 'queued' | 'interrupt') => within(screen.getByTestId('composer-mode')).getByRole('radio', { name: new RegExp(m === 'queued' ? 'Queued' : 'Interrupt') });

beforeEach(() => {
    editor.text.mockReset().mockResolvedValue('');
    editor.focus.mockClear();
    editor.reset.mockClear();
    keys.holder = null;
    pick.pending = false;
    seedPaneStore({
        sendDraft: vi.fn(async () => true), interruptQueued: vi.fn(async () => {}), setQueueBar: vi.fn(async () => {}),
        requestDraftContext: vi.fn(async () => {}), openConfirm: vi.fn(async () => true),
    });
});
afterEach(cleanup);

describe('AnnotationComposer', () => {
    it('Send sends the trimmed editor text and empties the editor; empty text says so and sends nothing', async () => {
        mount();
        editor.text.mockResolvedValue('  hello  ');
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(store().sendDraft).toHaveBeenCalledWith(CELL_ID, KEY, 'run-1', 'hello'));
        await vi.waitFor(() => expect(editor.reset).toHaveBeenCalled());
        expect(screen.getByTestId('composer-send').textContent).toBe('Send');
        editor.text.mockResolvedValue('   ');
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(screen.getByTestId('composer-notice').textContent).toBe('Write a message first.'));
        expect(store().sendDraft).toHaveBeenCalledTimes(1);
        vi.mocked(store().sendDraft).mockResolvedValueOnce(false);
        editor.text.mockResolvedValue('again');
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(store().sendDraft).toHaveBeenCalledTimes(2));
        expect(editor.reset).toHaveBeenCalledTimes(1);
    });

    it('text typed while the message was on its way stays in the editor', async () => {
        mount();
        editor.text.mockResolvedValueOnce('hello').mockResolvedValueOnce('hello and more');
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(editor.text).toHaveBeenCalledTimes(2));
        expect(store().sendDraft).toHaveBeenCalledWith(CELL_ID, KEY, 'run-1', 'hello');
        expect(editor.reset).not.toHaveBeenCalled();
        // An editor that cannot hand its text over defers to the draft: kept text keeps it, an emptied draft empties it.
        act(() => store().setComposerDraft(KEY, { text: 'typed meanwhile' }));
        editor.text.mockResolvedValueOnce('hello').mockRejectedValueOnce(new Error('timed out'));
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(editor.text).toHaveBeenCalledTimes(4));
        expect(editor.reset).not.toHaveBeenCalled();
        act(() => store().setComposerDraft(KEY, { text: '' }));
        editor.text.mockResolvedValueOnce('hello').mockRejectedValueOnce(new Error('timed out'));
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(editor.reset).toHaveBeenCalledTimes(1));
    });

    it('a draft still being saved says so; Send is inert while sending or while the run stops', async () => {
        mount();
        editor.text.mockRejectedValue(new Error('timed out'));
        fireEvent.click(screen.getByTestId('composer-send'));
        await vi.waitFor(() => expect(screen.getByTestId('composer-notice').textContent).toBe('Still saving the draft: timed out'));
        act(() => store().setComposerDraft(KEY, { sending: true, notice: null }));
        expect(screen.getByTestId('composer-send')).toHaveAttribute('aria-disabled', 'true');
        fireEvent.click(screen.getByTestId('composer-send'));
        expect(editor.text).toHaveBeenCalledTimes(1);
        cleanup();
        act(() => store().setComposerDraft(KEY, { sending: false }));
        mount({ stopping: true });
        expect(screen.getByTestId('composer-send')).toHaveAttribute('aria-disabled', 'true');
        expect(screen.getByTestId('composer-notice').textContent).toBe('This run is stopping.');
        fireEvent.click(screen.getByTestId('composer-send'));
        expect(editor.text).toHaveBeenCalledTimes(1);
    });

    it('Interrupt offers to interrupt the queued messages: Yes interrupts them, Cancel keeps them; the mode is Interrupt both ways', async () => {
        mount();
        expect(mode('queued')).toHaveAttribute('aria-checked', 'true');
        fireEvent.click(mode('interrupt'));
        await vi.waitFor(() => expect(store().interruptQueued).toHaveBeenCalledWith(CELL_ID, 'run-1', ['q1', 'q2']));
        expect(store().openConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Interrupt', body: 'Send the 2 queued messages as interrupt too?', confirmLabel: 'Yes', cancelLabel: 'Cancel', cellId: CELL_ID }));
        expect(draft().mode).toBe('interrupt');
        fireEvent.click(mode('interrupt'));
        expect(store().openConfirm).toHaveBeenCalledTimes(1);
        fireEvent.click(mode('queued'));
        expect(draft().mode).toBe('queued');
        vi.mocked(store().openConfirm).mockResolvedValueOnce(false);
        fireEvent.click(mode('interrupt'));
        await vi.waitFor(() => expect(store().openConfirm).toHaveBeenCalledTimes(2));
        expect(store().interruptQueued).toHaveBeenCalledTimes(1);
        expect(draft().mode).toBe('interrupt');
    });

    it('asks nothing with no queued message, and never in the All composer', async () => {
        mount({ context: ctx('run-1', [msg('i1', { mode: 'interrupt' })]) });
        fireEvent.click(mode('interrupt'));
        expect(store().openConfirm).not.toHaveBeenCalled();
        cleanup();
        mount({ scope: { kind: 'all' }, contextId: 'cell', context: ctx('cell', [msg('q', { context_id: 'cell' })]) });
        fireEvent.click(mode('interrupt'));
        expect(store().openConfirm).not.toHaveBeenCalled();
        expect(store().composerDrafts[`${CELL_ID}:all`].mode).toBe('interrupt');
        expect(screen.queryByTestId('queue-bar')).toBeNull();
        expect(screen.queryByTestId('composer-place')).toBeNull();
    });

    it('the queue bar toggles; with it on the message goes above or below it (never below as an interrupt)', () => {
        const { rerender } = mount();
        fireEvent.click(screen.getByTestId('queue-bar'));
        expect(store().setQueueBar).toHaveBeenCalledWith(CELL_ID, 'run-1', true);
        expect(screen.queryByTestId('composer-place')).toBeNull();
        rerender(<AnnotationComposer cellId={CELL_ID} channel="ch" scope={{ kind: 'run', runId: 'run-1' }} contextId="run-1" context={ctx('run-1', [], { bar_on: true })}
            barOn stopping={false} maximized={false} onEscape={() => {}} />);
        expect(screen.getByTestId('queue-bar')).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(screen.getByTestId('queue-bar'));
        expect(store().setQueueBar).toHaveBeenLastCalledWith(CELL_ID, 'run-1', false);
        const place = screen.getByTestId('composer-place');
        expect(place).toHaveAttribute('data-place', 'above');
        fireEvent.click(within(place).getByRole('radio', { name: 'Below bar' }));
        expect(place).toHaveAttribute('data-place', 'below');
        expect(draft().below).toBe(true);
        fireEvent.click(within(place).getByRole('radio', { name: 'Above bar' }));
        expect(draft().below).toBe(false);
        fireEvent.click(within(place).getByRole('radio', { name: 'Below bar' }));
        fireEvent.click(mode('interrupt'));
        expect(draft()).toMatchObject({ mode: 'interrupt', below: false });
        expect(place).toHaveAttribute('aria-disabled', 'true');
        expect(within(place).getByRole('radio', { name: 'Below bar' })).toBeDisabled();
    });

    it('Add annotation asks the file last viewed; it is inert without one, while waiting, or full', () => {
        mount();
        const add = screen.getByTestId('composer-add-annotation');
        expect(add.textContent).toBe('Add annotation');
        expect(add).toHaveAttribute('aria-label', 'Add annotation');
        expect(add).toHaveAttribute('aria-disabled', 'true');
        expect(add).toHaveAttribute('title', 'Open a file in this cell to pick from it');
        fireEvent.click(add);
        expect(store().requestDraftContext).not.toHaveBeenCalled();
        act(() => useCollimateStore.setState({ lastFileTab: { [CELL_ID]: 'site/page.html' } }));
        expect(add).toHaveAttribute('title', 'Pick from page.html');
        fireEvent.click(add);
        expect(store().requestDraftContext).toHaveBeenCalledWith(CELL_ID, KEY, 'site/page.html');
        act(() => useCollimateStore.setState({ annotationRequests: { [CELL_ID]: { draftKey: KEY, path: 'site/page.html', controller: new AbortController() } } }));
        expect(add).toHaveAttribute('aria-disabled', 'true');
        act(() => useCollimateStore.setState({ annotationRequests: {} }));
        const items = Array.from({ length: 16 }, (_, i) => ({ path: `f${i}`, renderer_id: 'r', label: `L${i}`, text: 't', anchor: null }));
        act(() => store().setComposerDraft(KEY, { contextItems: items }));
        expect(add).toHaveAttribute('aria-disabled', 'true');
        expect(add).toHaveAttribute('title', 'At most 16 attachments per message');
    });

    it('waits on the file with a cancel, shows the outcome\'s notice, and abandons the pick when it unmounts', () => {
        const cancel = vi.fn();
        useCollimateStore.setState({ cancelDraftContextRequest: cancel, annotationRequests: { [CELL_ID]: { draftKey: KEY, path: 'site/page.html', controller: new AbortController() } } } as never);
        const { unmount } = mount();
        const waiting = screen.getByTestId('composer-waiting');
        expect(waiting.textContent).toContain('waiting on page.html');
        expect(waiting.querySelector('.border-teal')).not.toBeNull();
        fireEvent.click(screen.getByTestId('composer-waiting-cancel'));
        expect(cancel).toHaveBeenCalledWith(CELL_ID);
        act(() => store().setComposerDraft(KEY, { notice: 'page.html’s renderer does not take annotations.' }));
        expect(screen.getByTestId('composer-notice')).toHaveAttribute('role', 'alert');
        expect(screen.getByTestId('composer-notice').textContent).toBe('page.html’s renderer does not take annotations.');
        unmount();
        expect(cancel).toHaveBeenCalledTimes(2);
        useCollimateStore.setState({ annotationRequests: { [CELL_ID]: { draftKey: 'other', path: 'p', controller: new AbortController() } } });
        mount().unmount();
        expect(cancel).toHaveBeenCalledTimes(2);
    });

    it('each attached part is a chip reading its label, with a remove button; one with text shows it, to read only', () => {
        mount();
        act(() => store().setComposerDraft(KEY, { contextItems: [
            { path: 'page.html', renderer_id: 'html', label: 'page.html › #a', text: '', anchor: null },
            { path: 'page.html', renderer_id: 'html', label: 'page.html › #b', text: 'B', anchor: null },
            { path: 'notes.md', renderer_id: 'milkdown', label: 'notes.md › selection', text: 'the first\nparagraph', anchor: null },
        ] }));
        const chips = screen.getAllByTestId('composer-context-chip');
        expect(chips.map(c => within(c).getByText(/›/).textContent)).toEqual(['page.html › #a', 'page.html › #b', 'notes.md › selection']);
        expect(chips[0]).toHaveAttribute('title', 'page.html');
        expect(within(chips[0]).queryByTestId('composer-context-text')).toBeNull();
        expect(within(chips[1]).getByTestId('composer-context-text').textContent).toBe('B');
        const block = within(chips[2]).getByTestId('composer-context-text');
        expect(block.textContent).toBe('the first\nparagraph');
        expect(block.querySelector('textarea, input, [contenteditable]')).toBeNull();
        expect(block.getAttribute('contenteditable')).toBeNull();
        fireEvent.click(within(chips[0]).getByTestId('composer-context-remove'));
        expect(draft().contextItems.map(i => i.label)).toEqual(['page.html › #b', 'notes.md › selection']);
        fireEvent.click(within(screen.getAllByTestId('composer-context-chip')[1]).getByTestId('composer-context-remove'));
        expect(draft().contextItems.map(i => i.label)).toEqual(['page.html › #b']);
    });

    it('the Add annotation press leaves the focus, and the file view’s selection, where they are', () => {
        mount();
        act(() => useCollimateStore.setState({ lastFileTab: { [CELL_ID]: 'notes.md' } }));
        expect(fireEvent.mouseDown(screen.getByTestId('composer-add-annotation'))).toBe(false);
    });

    it('expand fills the pane and back', () => {
        mount();
        const root = screen.getByTestId('annotation-composer');
        expect(root).toHaveAttribute('data-expanded', 'false');
        fireEvent.click(screen.getByTestId('composer-expand'));
        expect(root).toHaveAttribute('data-expanded', 'true');
        expect(root.className).toContain('flex-1');
        fireEvent.click(screen.getByTestId('composer-expand'));
        expect(root).toHaveAttribute('data-expanded', 'false');
    });

    it('the edit and reply banners show, and cancel them', () => {
        mount();
        act(() => store().loadDraftForEdit(KEY, msg('m1', { body: 'old' })));
        expect(screen.getByTestId('composer-editing').textContent).toContain('Editing a message');
        expect(screen.getByTestId('composer-send').textContent).toBe('Save');
        fireEvent.click(screen.getByTestId('composer-edit-cancel'));
        expect(screen.queryByTestId('composer-editing')).toBeNull();
        act(() => store().setDraftReply(KEY, msg('m2', { body: 'what about this' })));
        expect(screen.getByTestId('composer-reply-to').textContent).toContain('Replying to You: what about this');
        fireEvent.click(screen.getByTestId('composer-reply-cancel'));
        expect(screen.queryByTestId('composer-reply-to')).toBeNull();
    });

    it('typing mirrors the text to the draft and clears the notice', () => {
        mount();
        act(() => store().setComposerDraft(KEY, { notice: 'x' }));
        fireEvent.change(screen.getByTestId('fake-editor'), { target: { value: 'typed' } });
        expect(draft()).toMatchObject({ text: 'typed', notice: null });
    });

    it('Esc in a native editor collapses first, then leaves, never reaching a window listener; a pending pick keeps it', () => {
        const { onEscape } = mount();
        const reached = vi.fn();
        window.addEventListener('keydown', reached);
        try {
            act(() => store().setComposerDraft(KEY, { expanded: true }));
            fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
            expect(draft().expanded).toBe(false);
            expect(onEscape).not.toHaveBeenCalled();
            fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
            expect(onEscape).toHaveBeenCalledTimes(1);
            pick.pending = true;
            fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'Escape' });
            expect(onEscape).toHaveBeenCalledTimes(1);
            fireEvent.keyDown(screen.getByTestId('fake-editor'), { key: 'a' });
        } finally {
            window.removeEventListener('keydown', reached);
        }
        // The pick's Esc and the plain key go on; the composer's own Escs did not.
        expect(reached).toHaveBeenCalledTimes(2);
    });

    it('a window Esc forwarded from the editor\'s frame is the composer\'s, before the tab\'s own listener; keys elsewhere or a pick keep it', () => {
        const { onEscape } = mount();
        const tabClose = vi.fn();
        window.addEventListener('keydown', tabClose);
        try {
            keys.holder = screen.getByTestId('fake-editor');
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
            expect(onEscape).toHaveBeenCalledTimes(1);
            expect(tabClose).not.toHaveBeenCalled();
            pick.pending = true;
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
            pick.pending = false;
            keys.holder = document.body;
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
            expect(onEscape).toHaveBeenCalledTimes(1);
            expect(tabClose).toHaveBeenCalledTimes(3);
        } finally {
            window.removeEventListener('keydown', tabClose);
        }
    });

    it('a forwarded Esc is only the composer whose editor holds the keys, however many are mounted', () => {
        const run = vi.fn();
        const all = vi.fn();
        render(<>
            <AnnotationComposer cellId={CELL_ID} channel="ch" scope={{ kind: 'run', runId: 'run-1' }} contextId="run-1" context={undefined}
                barOn={false} stopping={false} maximized={false} onEscape={run} />
            <AnnotationComposer cellId={CELL_ID} channel="ch" scope={{ kind: 'all' }} contextId="cell" context={undefined}
                barOn={false} stopping={false} maximized={false} onEscape={all} />
        </>);
        const [first, second] = screen.getAllByTestId('annotation-composer');
        keys.holder = within(second).getByTestId('fake-editor');
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        expect([run.mock.calls.length, all.mock.calls.length]).toEqual([0, 1]);
        keys.holder = within(first).getByTestId('fake-editor');
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        expect([run.mock.calls.length, all.mock.calls.length]).toEqual([1, 1]);
    });

    it('focus() hands the keys to the editor', () => {
        let handle: { focus: () => void } | null = null;
        render(<AnnotationComposer ref={(h) => { handle = h; }} cellId={CELL_ID} channel="ch" scope={{ kind: 'all' }} contextId="cell" context={undefined}
            barOn={false} stopping={false} maximized={false} onEscape={() => {}} />);
        handle!.focus();
        expect(editor.focus).toHaveBeenCalled();
    });
});
