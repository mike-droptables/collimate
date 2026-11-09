// The annotation routes' client: each wrapper's method, URL (encoded) and
// body, the 404s that answer null, and the refusal's code and sentence.

import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import {
    annotationErrorCode, annotationErrorText, deleteAnnotation, fetchAnnotationContext, fetchCellAnnotations, fetchFileTouches,
    fetchRunInbox, interruptAnnotations, orderAnnotations, patchAnnotation, postAnnotation, sendAnnotationToRun, setAnnotationBar,
} from './annotations';

const message = { id: 'a_1', context_id: 'r 1', parent_id: null, author: 'user', author_label: null, kind: 'message', body: 'hi',
    context_items: [], refs: [], mode: 'queued', position: 1, held: false, revision: 1, created_at: '2026-10-24 10:00:00',
    edited_at: null, read_at: null };
const context = { id: 'r 1', cell_id: 'c/1', run_id: 'r 1', label: 'agent-1', bar_on: false, listening: true, interrupt_seq: 0, rev: 3,
    sealed: false, messages: [message] };
const refusal = (status: number, data: unknown) => Object.assign(new Error('refused'), { response: { status, data } });

const answer = (data: unknown) => vi.spyOn(api, 'request').mockResolvedValue({ data });
const call = (request: ReturnType<typeof answer>) => request.mock.calls[0][0] as { method: string; url: string; data?: unknown };
const B = '/channels/ch%201/cells/c%2F1/annotations';

afterEach(() => vi.restoreAllMocks());

describe('the annotation wrappers', () => {
    it('lists the cell\'s contexts, dropping an unreadable one', async () => {
        const request = answer({ contexts: [context, { id: 'bad' }] });
        expect((await fetchCellAnnotations('ch 1', 'c/1')).map(c => c.id)).toEqual(['r 1']);
        expect(call(request)).toMatchObject({ method: 'GET', url: B });
        answer({});
        expect(await fetchCellAnnotations('ch 1', 'c/1')).toEqual([]);
    });

    it('reads one context, null on a 404, and throws anything else', async () => {
        const request = answer(context);
        expect((await fetchAnnotationContext('ch 1', 'c/1', 'r 1'))?.rev).toBe(3);
        expect(call(request)).toMatchObject({ method: 'GET', url: `${B}/r%201` });
        vi.restoreAllMocks();
        vi.spyOn(api, 'request').mockRejectedValueOnce(refusal(404, { detail: 'No such annotation context' }));
        expect(await fetchAnnotationContext('ch 1', 'c/1', 'gone')).toBeNull();
        vi.spyOn(api, 'request').mockRejectedValueOnce(refusal(500, {}));
        await expect(fetchAnnotationContext('ch 1', 'c/1', 'x')).rejects.toThrow('refused');
        vi.spyOn(api, 'request').mockResolvedValueOnce({ data: { nope: 1 } });
        await expect(fetchAnnotationContext('ch 1', 'c/1', 'x')).rejects.toThrow(/unreadable annotation context/);
    });

    it('posts, patches, deletes and sends a message', async () => {
        let request = answer(message);
        const body = { operation_id: 'op', context_id: 'r 1', body: 'hi', mode: 'queued' as const, held: true };
        expect((await postAnnotation('ch 1', 'c/1', body)).id).toBe('a_1');
        expect(call(request)).toMatchObject({ method: 'POST', url: B, data: body });
        vi.restoreAllMocks();
        request = answer(message);
        await patchAnnotation('ch 1', 'c/1', 'a/1', { expected_revision: 2, body: 'new' });
        expect(call(request)).toMatchObject({ method: 'PATCH', url: `${B}/a%2F1`, data: { expected_revision: 2, body: 'new' } });
        vi.restoreAllMocks();
        request = answer({ ok: true });
        await deleteAnnotation('ch 1', 'c/1', 'a/1', 4);
        expect(call(request)).toMatchObject({ method: 'DELETE', url: `${B}/a%2F1?expected_revision=4` });
        vi.restoreAllMocks();
        request = answer(message);
        await sendAnnotationToRun('ch 1', 'c/1', 'a/1', 'r 2');
        expect(call(request)).toMatchObject({ method: 'POST', url: `${B}/a%2F1/send`, data: { run_id: 'r 2' } });
        vi.restoreAllMocks();
        answer({ id: 'x' });
        await expect(postAnnotation('ch 1', 'c/1', body)).rejects.toThrow(/unreadable message/);
    });

    it('orders, sets the bar and interrupts, each answering the context', async () => {
        let request = answer(context);
        expect((await orderAnnotations('ch 1', 'c/1', 'r 1', ['a', 'b'], 1)).id).toBe('r 1');
        expect(call(request)).toMatchObject({ method: 'PUT', url: `${B}/r%201/order`, data: { ids: ['a', 'b'], held_from: 1 } });
        vi.restoreAllMocks();
        request = answer(context);
        await orderAnnotations('ch 1', 'c/1', 'r 1', ['a']);
        expect(call(request).data).toEqual({ ids: ['a'] });
        vi.restoreAllMocks();
        request = answer(context);
        await setAnnotationBar('ch 1', 'c/1', 'r 1', true);
        expect(call(request)).toMatchObject({ method: 'PUT', url: `${B}/r%201/bar`, data: { on: true } });
        vi.restoreAllMocks();
        request = answer(context);
        await interruptAnnotations('ch 1', 'c/1', 'r 1', ['a']);
        expect(call(request)).toMatchObject({ method: 'POST', url: `${B}/r%201/interrupt`, data: { ids: ['a'] } });
    });

    it('reads a run\'s file touches and its inbox (null on a 404 or a bad answer)', async () => {
        let request = answer({ touches: [{ at: 'x', path: 'a.md', op: 'edit', folder: false, version_id: 9 }, { path: 'bad' }] });
        expect(await fetchFileTouches('ch 1', 'r/1')).toEqual([{ at: 'x', path: 'a.md', op: 'edit', folder: false, version_id: '9' }]);
        expect(call(request)).toMatchObject({ method: 'GET', url: '/channels/ch%201/runs/r%2F1/file_touches' });
        vi.restoreAllMocks();
        answer({});
        expect(await fetchFileTouches('ch', 'r')).toEqual([]);
        vi.restoreAllMocks();
        request = answer({ chat_id: 'c', ws_path: '/api/chats/c', token: 't' });
        expect(await fetchRunInbox('ch 1', 'r/1')).toEqual({ chat_id: 'c', ws_path: '/api/chats/c', token: 't' });
        expect(call(request)).toMatchObject({ method: 'GET', url: '/channels/ch%201/runs/r%2F1/inbox' });
        vi.restoreAllMocks();
        answer({ chat_id: 'c' });
        expect(await fetchRunInbox('ch', 'r')).toBeNull();
        vi.restoreAllMocks();
        vi.spyOn(api, 'request').mockRejectedValueOnce(refusal(404, {}));
        expect(await fetchRunInbox('ch', 'r')).toBeNull();
        vi.spyOn(api, 'request').mockRejectedValueOnce(refusal(502, {}));
        await expect(fetchRunInbox('ch', 'r')).rejects.toThrow('refused');
    });
});

describe('refusals', () => {
    it('the code is response.data.error at any status, else null', () => {
        expect(annotationErrorCode(refusal(409, { error: 'sealed', detail: 'This run no longer takes messages.' }))).toBe('sealed');
        expect(annotationErrorCode(refusal(413, { error: 'too_large', detail: 'x' }))).toBe('too_large');
        expect(annotationErrorCode(refusal(409, { detail: 'sealed' }))).toBeNull();
        expect(annotationErrorCode(new Error('down'))).toBeNull();
        expect(annotationErrorCode(null)).toBeNull();
    });

    it('known codes read as the pane\'s sentences; others as the server\'s detail, else the error', () => {
        const text = (code: string) => annotationErrorText(refusal(409, { error: code, detail: 'server words' }));
        expect(text('sealed')).toBe('This run has ended; your message was not sent.');
        expect(text('locked')).toBe('The run has already read this message; it can no longer change.');
        expect(text('revision')).toBe('This message changed elsewhere; look again and retry.');
        for (const code of ['parent_not_root', 'unknown_message', 'unknown_context']) expect(text(code)).toBe('The message you answered is gone.');
        expect(text('run_not_live')).toBe('That run is no longer live.');
        expect(text('stale')).toBe('The queue changed elsewhere; look again and retry.');
        expect(text('bar_off')).toBe('Turn the queue bar on first.');
        expect(text('too_large')).toBe('The message is over 64 KiB.');
        expect(text('empty_text')).toBe('Write a message first.');
        expect(text('full')).toBe('This context holds 2000 messages; delete some first.');
        expect(text('operation_reused')).toMatch(/already sent/);
        expect(text('invalid')).toMatch(/could not read/);
        expect(text('not_root')).toBe('server words');
        expect(annotationErrorText(new Error('Network Error'))).toBe('Network Error');
    });
});
