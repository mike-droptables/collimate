// The composer's in-memory document (lib/messageDraft): FileDocument's
// view-acknowledged edit rules without persistence.

import { describe, expect, it, vi } from 'vitest';
import { MessageDraft } from '../messageDraft';
import type { RendererRecoverySnapshot } from '../fileSnapshotCodec';

const snap = (snapshotId: string): RendererRecoverySnapshot => ({ codecHash: 'c', snapshotId, payload: snapshotId });

describe('MessageDraft', () => {
    it('starts from its body as one owner, and an edit replaces the content', () => {
        const draft = new MessageDraft('hello', async () => '');
        expect(draft.getSnapshot()).toEqual({ ownerId: draft.id, content: 'hello', editSeq: 0, materializedSeq: 0, viewAcks: {} });
        const listener = vi.fn();
        const off = draft.subscribe(listener);
        draft.edit('hello there', { viewId: 'v', viewSeq: 1 });
        expect(draft.getSnapshot()).toMatchObject({ content: 'hello there', editSeq: 1, materializedSeq: 1, viewAcks: { v: 1 } });
        expect(listener).toHaveBeenCalledTimes(1);
        off();
        draft.edit('again');
        expect(listener).toHaveBeenCalledTimes(1);
        expect(new MessageDraft().getSnapshot().content).toBe('');
    });

    it('ignores a stale view seq; the same body only acknowledges the view', () => {
        const draft = new MessageDraft('a', async () => '');
        draft.edit('b', { viewId: 'v', viewSeq: 2 });
        draft.edit('c', { viewId: 'v', viewSeq: 2 });
        draft.edit('d', { viewId: 'v', viewSeq: 1 });
        draft.edit('e', { viewId: '', viewSeq: 3 });
        expect(draft.getSnapshot().content).toBe('b');
        draft.edit('b', { viewId: 'v', viewSeq: 3 });
        expect(draft.getSnapshot()).toMatchObject({ content: 'b', editSeq: 1, viewAcks: { v: 3 } });
        draft.editSnapshot(snap('s'), { viewId: 'v', viewSeq: 3 });
        expect(draft.getSnapshot().recovery).toBeUndefined();
    });

    it('derives a snapshot edit’s text in order, and materializedBody waits for the latest', async () => {
        let release: (() => void) | null = null;
        const derive = vi.fn((s: RendererRecoverySnapshot) => new Promise<string>(resolve => {
            release = () => resolve(`text of ${s.snapshotId}`);
        }));
        const draft = new MessageDraft('', derive);
        draft.editSnapshot(snap('s1'), { viewId: 'v', viewSeq: 1 });
        expect(draft.getSnapshot()).toMatchObject({ editSeq: 1, materializedSeq: 0 });
        draft.editSnapshot(snap('s1'), { viewId: 'v', viewSeq: 2 });
        expect(draft.getSnapshot()).toMatchObject({ editSeq: 1, viewAcks: { v: 2 } });
        const body = draft.materializedBody();
        draft.editSnapshot(snap('s2'), { viewId: 'v', viewSeq: 3 });
        await vi.waitFor(() => expect(release).not.toBeNull());
        release!();
        await vi.waitFor(() => expect(derive).toHaveBeenCalledTimes(2));
        release!();
        expect(await body).toBe('text of s2');
        expect(draft.getSnapshot()).toMatchObject({ content: 'text of s2', editSeq: 2, materializedSeq: 2 });
        expect(await draft.materializedBody()).toBe('text of s2');
    });

    it('a reset mid-derive wins, and keeps the view acks so the typing view accepts it', async () => {
        let release: (() => void) | null = null;
        const draft = new MessageDraft('', () => new Promise<string>(resolve => { release = () => resolve('late'); }));
        draft.editSnapshot(snap('s1'), { viewId: 'v', viewSeq: 4 });
        const body = draft.materializedBody();
        draft.reset();
        await vi.waitFor(() => expect(release).not.toBeNull());
        release!();
        expect(await body).toBe('');
        expect(draft.getSnapshot()).toMatchObject({ content: '', recovery: undefined, editSeq: 2, materializedSeq: 2, viewAcks: { v: 4 } });
        draft.reset('again');
        expect(draft.getSnapshot()).toMatchObject({ content: 'again', editSeq: 3, materializedSeq: 3 });
    });
});
