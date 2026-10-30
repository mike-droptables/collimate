// The composer's in-memory document: what a FileDocument is to a cell
// file, minus persistence. The renderer host hands it to the editor as the
// document snapshot (ownerId, content, recovery, edit sequences, view acks)
// and feeds the editor's edits back through `edit` / `editSnapshot`, so a
// re-created pooled frame reloads the live draft and a host replace (the
// reset after Send) is accepted by the view that typed it.

import { useSyncExternalStore } from 'react';
import type { DocumentViewEdit } from './fileDocument';
import { materializeSnapshot, type RendererRecoverySnapshot } from './fileSnapshotCodec';

export interface MessageDraftSnapshot {
    ownerId: string;
    content: string;
    recovery?: RendererRecoverySnapshot;
    editSeq: number;
    materializedSeq: number;
    viewAcks: Readonly<Record<string, number>>;
}

export class MessageDraft {
    /** The document owner the views see. */
    readonly id = crypto.randomUUID();
    private snapshot: MessageDraftSnapshot;
    private listeners = new Set<() => void>();
    private materializing: Promise<void> | null = null;

    constructor(body = '', private derive: (s: RendererRecoverySnapshot) => Promise<string> = materializeSnapshot) {
        this.snapshot = { ownerId: this.id, content: body, editSeq: 0, materializedSeq: 0, viewAcks: {} };
    }

    getSnapshot = (): MessageDraftSnapshot => this.snapshot;

    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => { this.listeners.delete(listener); };
    };

    private update(patch: Partial<MessageDraftSnapshot>): void {
        this.snapshot = { ...this.snapshot, ...patch };
        for (const listener of this.listeners) listener();
    }

    /** The acks after a view's edit; null for a stale one (a seq the view
     *  already sent). */
    private acknowledge(view?: DocumentViewEdit): Readonly<Record<string, number>> | null {
        if (!view) return this.snapshot.viewAcks;
        if (!view.viewId || !Number.isSafeInteger(view.viewSeq) || view.viewSeq < 1
            || view.viewSeq <= (this.snapshot.viewAcks[view.viewId] ?? 0)) return null;
        return { ...this.snapshot.viewAcks, [view.viewId]: view.viewSeq };
    }

    /** A text edit: the body replaces the content and any recovery. */
    edit(body: string, view?: DocumentViewEdit): void {
        const viewAcks = this.acknowledge(view);
        if (!viewAcks) return;
        if (body === this.snapshot.content) {
            if (view) this.update({ viewAcks });
            return;
        }
        const seq = this.snapshot.editSeq + 1;
        this.update({ content: body, viewAcks, recovery: undefined, editSeq: seq, materializedSeq: seq });
    }

    /** A recovery-snapshot edit: kept as is, its text derived behind it. */
    editSnapshot(recovery: RendererRecoverySnapshot, view?: DocumentViewEdit): void {
        const viewAcks = this.acknowledge(view);
        if (!viewAcks) return;
        if (recovery.snapshotId === this.snapshot.recovery?.snapshotId) {
            if (view) this.update({ viewAcks });
            return;
        }
        this.update({ recovery, viewAcks, editSeq: this.snapshot.editSeq + 1 });
        void this.materializedBody().catch(() => undefined);
    }

    /** The text of the latest edit, once every recovery snapshot up to it
     *  is derived. */
    async materializedBody(): Promise<string> {
        if (this.materializing) {
            await this.materializing;
            return this.materializedBody();
        }
        if (!this.snapshot.recovery || this.snapshot.materializedSeq >= this.snapshot.editSeq) return this.snapshot.content;
        const pending = (async () => {
            while (this.snapshot.recovery && this.snapshot.materializedSeq < this.snapshot.editSeq) {
                const recovery = this.snapshot.recovery, seq = this.snapshot.editSeq;
                const body = await this.derive(recovery);
                if (!this.snapshot.recovery || seq <= this.snapshot.materializedSeq) continue;
                this.update({ content: body, materializedSeq: seq });
            }
        })();
        this.materializing = pending;
        try {
            await pending;
        } finally {
            if (this.materializing === pending) this.materializing = null;
        }
        return this.snapshot.content;
    }

    /** The host replaces the draft (cleared after Send). The view acks are
     *  kept, so the view that typed accepts the replacement. */
    reset(body = ''): void {
        const seq = this.snapshot.editSeq + 1;
        this.update({ content: body, recovery: undefined, editSeq: seq, materializedSeq: seq });
    }
}

/** The draft's snapshot, re-rendering on every change. */
export function useMessageDraft(draft: MessageDraft): MessageDraftSnapshot {
    return useSyncExternalStore(draft.subscribe, draft.getSnapshot, draft.getSnapshot);
}
