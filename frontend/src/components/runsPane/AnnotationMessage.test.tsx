// One message of a status pane timeline: its head (author, tag, prompt and
// interrupt marks, time, edited), its markdown body with cell links, its
// context chips, its read lock, its replies, and its actions by lock rule.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const reveal = vi.hoisted(() => vi.fn());
vi.mock('@/lib/revealFile', () => ({ revealCellFile: reveal }));

import { AnnotationMessage, type AnnotationMessageProps } from './AnnotationMessage';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { clockOf } from '@/lib/cellLog';
import { serverTime, type Thread } from '@/lib/annotations';
import { CELL_ID, RUN, RUN_2, finishedRun, msg, seedPaneStore } from '@/test/annotationFixtures';

const store = () => useCollimateStore.getState();
const thread = (root = msg('m1'), replies = [] as Thread['replies']): Thread => ({ root, replies });
const mount = (props: Partial<AnnotationMessageProps> = {}) => render(
    <AnnotationMessage cellId={CELL_ID} channel="ch" thread={thread()} tag={null} sendable={false} readOnly={false}
        draftKey="k" reader="Agent 11111111" {...props} />,
);
const messages = () => screen.getAllByTestId('annotation-message');

beforeEach(() => {
    reveal.mockReset();
    seedPaneStore({ openConfirm: vi.fn(async () => true), deleteMessage: vi.fn(async () => {}), sendMessageToRun: vi.fn(async () => {}) });
});
afterEach(cleanup);

describe('AnnotationMessage', () => {
    it('heads a user message as You, with its tag, interrupt mark, time and edited marker', () => {
        mount({ thread: thread(msg('m1', { mode: 'interrupt', edited_at: '2026-10-24 10:01:00', created_at: '2026-10-24 10:00:00' })), tag: 'agent-11111111' });
        const m = messages()[0];
        expect(m).toHaveAttribute('data-id', 'm1');
        expect(m).toHaveAttribute('data-read', 'false');
        expect(m).toHaveAttribute('data-author', 'user');
        expect(m).toHaveAttribute('data-held', 'false');
        expect(m.textContent).toContain('You');
        expect(within(m).getByTestId('message-tag').textContent).toBe('agent-11111111');
        expect(within(m).getByTestId('message-mode')).toBeInTheDocument();
        expect(m.textContent).toContain(clockOf(serverTime('2026-10-24 10:00:00')));
        expect(m.textContent).toContain('edited');
    });

    it('names a run\'s post by its run, live, finished or unknown', () => {
        seedPaneStore({ runs: { [RUN.runId]: RUN }, finishedRuns: { [CELL_ID]: [finishedRun(RUN_2)] } });
        mount({ thread: thread(msg('p1', { author: 'run', author_label: RUN.label, read_at: 'x' })) });
        expect(messages()[0].textContent).toContain('Agent 11111111');
        cleanup();
        mount({ thread: thread(msg('p2', { author: 'run', author_label: RUN_2.label, read_at: 'x' })) });
        expect(messages()[0].textContent).toContain('Chat 22222222');
        cleanup();
        mount({ thread: thread(msg('p3', { author: 'run', author_label: 'ghost-33333333', read_at: 'x' })) });
        expect(messages()[0].textContent).toContain('ghost-33333333');
        cleanup();
        mount({ thread: thread(msg('p4', { author: 'run', read_at: 'x' })) });
        expect(messages()[0].textContent).toMatch(/^Run/);
    });

    it('renders the body as markdown: a cell link opens the file in a new tab; a relative image loads from the cell', () => {
        mount({ thread: thread(msg('m1', { body: 'See [the page](page.html) and ![pic](img/a.png) or [web](https://example.test)' })) });
        const link = screen.getByRole('link', { name: 'the page' });
        fireEvent.click(link);
        expect(reveal).toHaveBeenCalledWith(CELL_ID, 'page.html', { newTab: true });
        expect(screen.getByRole('link', { name: 'web' })).toHaveAttribute('target', '_blank');
        expect(screen.getByRole('img', { name: 'pic' }).getAttribute('src')).toMatch(/\/channels\/ch\/cells\/c1\/files\/img\/a\.png$/);
    });

    it('a context chip reads its label, the path in its title, and opens to its text and Open file', () => {
        mount({ thread: thread(msg('m1', { context_items: [{ path: 'page.html', renderer_id: 'html', label: 'page.html › #x', text: '<b>el</b>', anchor: null }] })) });
        const chip = screen.getByTestId('annotation-context-chip');
        expect(chip.textContent).toBe('page.html › #x');
        expect(chip).toHaveAttribute('title', 'page.html');
        expect(chip).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(chip);
        expect(chip).toHaveAttribute('aria-expanded', 'true');
        expect(screen.getByText('<b>el</b>')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Open file' }));
        expect(reveal).toHaveBeenCalledWith(CELL_ID, 'page.html', { newTab: true });
    });

    it('a read message shows who read it and when; a prompt says it was read at the start; neither has edit or delete', () => {
        mount({ thread: thread(msg('m1', { read_at: '2026-10-24 10:05:00' })) });
        expect(screen.getByTestId('message-read-lock').textContent).toBe(`Read by Agent 11111111 at ${clockOf(serverTime('2026-10-24 10:05:00'))}`);
        expect(screen.queryByTestId('message-edit')).toBeNull();
        expect(screen.queryByTestId('message-delete')).toBeNull();
        expect(screen.getByTestId('message-reply')).toBeInTheDocument();
        cleanup();
        mount({ thread: thread(msg('p', { kind: 'prompt', read_at: 'x' })) });
        expect(screen.getByTestId('message-read-lock').textContent).toBe('Prompt · read at start');
        expect(screen.getByText('Prompt')).toBeInTheDocument();
        expect(screen.queryByTestId('message-edit')).toBeNull();
    });

    it('edit loads the message into the composer, first asking to discard a draft that holds text', async () => {
        mount();
        fireEvent.click(screen.getByTestId('message-edit'));
        await vi.waitFor(() => expect(store().composerDrafts.k?.editing?.id).toBe('m1'));
        expect(store().openConfirm).not.toHaveBeenCalled();
        act(() => store().setComposerDraft('k', { text: 'half a thought', editing: null }));
        vi.mocked(store().openConfirm).mockResolvedValueOnce(false);
        fireEvent.click(screen.getByTestId('message-edit'));
        await vi.waitFor(() => expect(store().openConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Edit message', confirmLabel: 'Discard', cellId: CELL_ID })));
        expect(store().composerDrafts.k.editing).toBeNull();
        fireEvent.click(screen.getByTestId('message-edit'));
        await vi.waitFor(() => expect(store().composerDrafts.k.editing?.id).toBe('m1'));
    });

    it('delete confirms (naming the replies it takes) then deletes', async () => {
        mount({ thread: thread(msg('m1'), [msg('r1', { parent_id: 'm1' }), msg('r2', { parent_id: 'm1' })]) });
        fireEvent.click(within(messages()[0]).getByTestId('message-delete'));
        await vi.waitFor(() => expect(store().deleteMessage).toHaveBeenCalledWith(CELL_ID, expect.objectContaining({ id: 'm1' })));
        expect(store().openConfirm).toHaveBeenCalledWith(expect.objectContaining({ body: 'Delete this message and its 2 replies?', destructive: true }));
        vi.mocked(store().openConfirm).mockResolvedValueOnce(false);
        fireEvent.click(within(messages()[1]).getByTestId('message-delete'));
        await vi.waitFor(() => expect(store().openConfirm).toHaveBeenLastCalledWith(expect.objectContaining({ body: 'Delete this message?' })));
        expect(store().deleteMessage).toHaveBeenCalledTimes(1);
    });

    it('replies sit one level under the root, with no reply or send-to of their own', () => {
        mount({ sendable: true, thread: thread(msg('m1', { context_id: 'cell' }), [msg('r1', { parent_id: 'm1', context_id: 'cell' })]) });
        const [root, reply] = messages();
        expect(reply).toHaveAttribute('data-reply', 'true');
        expect(reply.parentElement!.className).toContain('border-l');
        expect(within(root).getByTestId('message-reply')).toBeInTheDocument();
        expect(within(reply).queryByTestId('message-reply')).toBeNull();
        expect(within(reply).queryByTestId('message-send-to')).toBeNull();
        expect(within(reply).getByTestId('message-edit')).toBeInTheDocument();
        fireEvent.click(within(root).getByTestId('message-reply'));
        expect(store().composerDrafts.k.replyTo).toMatchObject({ id: 'm1', author: 'You' });
    });

    it('read-only: no actions but the chips', () => {
        mount({ readOnly: true });
        for (const id of ['message-edit', 'message-delete', 'message-reply']) expect(screen.queryByTestId(id)).toBeNull();
    });

    it('a cell note is sent to a live run from its menu; with no live run the button is inert', async () => {
        mount({ sendable: true, thread: thread(msg('n1', { context_id: 'cell' })) });
        const send = screen.getByTestId('message-send-to');
        expect(send).toHaveAttribute('aria-disabled', 'true');
        expect(send).toHaveAttribute('title', 'No run is live in this cell');
        fireEvent.click(send);
        expect(screen.queryByRole('menu')).toBeNull();
        act(() => useCollimateStore.setState({ runs: { [RUN.runId]: RUN, [RUN_2.runId]: RUN_2 } }));
        expect(send).not.toHaveAttribute('aria-disabled');
        fireEvent.click(send);
        const menu = screen.getByRole('menu', { name: 'Send to run' });
        fireEvent.click(within(menu).getByText('Chat 22222222'));
        await vi.waitFor(() => expect(store().sendMessageToRun).toHaveBeenCalledWith(CELL_ID, 'n1', RUN_2.runId));
    });

    it('a drag handle passed in renders among the actions', () => {
        mount({ dragHandle: <button type="button" data-testid="message-drag">grip</button> });
        expect(screen.getByTestId('message-drag')).toBeInTheDocument();
    });
});
