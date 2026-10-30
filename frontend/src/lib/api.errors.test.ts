import { routeApiToFetchStub } from '@/test/apiTransport';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { describeApiError, postRun } from './api';

describe('describeApiError', () => {
    it('prefers the server\'s detail sentence over the status line', () => {
        const error = Object.assign(new Error('Request failed with status code 409'), {
            response: { data: { detail: "A channel named 'test' already exists" } },
        });
        expect(describeApiError(error)).toBe("A channel named 'test' already exists");
    });
    it('reads a validation error\'s first message', () => {
        const error = Object.assign(new Error('Request failed with status code 422'), {
            response: { data: { detail: [{ msg: 'name is required', loc: ['query', 'name'] }] } },
        });
        expect(describeApiError(error)).toBe('name is required');
    });
    it('falls back to the error message', () => {
        expect(describeApiError(new Error('Network Error'))).toBe('Network Error');
        expect(describeApiError('boom')).toBe('boom');
    });
});

describe('postRun', () => {
    afterEach(() => { vi.unstubAllGlobals(); });
    it('turns a network failure into the connection sentence', async () => {
        vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
        const restore = routeApiToFetchStub();
        try { await expect(postRun('c', { runner: 'r', cellId: 'x' })).rejects.toThrow(
            "Couldn't reach the server — check your connection.",
        ); } finally { restore(); }
    });
    const refuse = (status: number, body: unknown) => vi.fn().mockResolvedValue(
        new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }));
    it('reads a surface another run holds as its own sentence (GN-03)', async () => {
        const restore = routeApiToFetchStub();
        try {
            vi.stubGlobal('fetch', refuse(409, {
                error: 'surface_taken', owner: 'chat-aaaaaaaa', run_id: 'r1', path: 'status.chat',
                message: 'status.chat belongs to chat-aaaaaaaa; choose another path or Run in new cell',
            }));
            await expect(postRun('c', { runner: 'r', cellId: 'x' })).rejects.toThrow(
                'status.chat belongs to chat-aaaaaaaa; choose another path or Run in new cell');
        } finally { restore(); }
    });
    it('reads a selected file gone from the cell as its own sentence (GN-01)', async () => {
        const restore = routeApiToFetchStub();
        try {
            vi.stubGlobal('fetch', refuse(409, { error: 'selected_file_missing' }));
            await expect(postRun('c', { runner: 'r', cellId: 'x', selectedFile: 'gone.md' })).rejects.toThrow(
                'The selected file is no longer in this cell — select another file and run again.');
        } finally { restore(); }
    });
});
