import { beforeEach, afterEach, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({
    replies: [] as { status: number; data: unknown }[],
    requests: [] as { method: string; url: string; data?: unknown }[],
}));
vi.mock('axios', async importOriginal => {
    const actual = await importOriginal<typeof import('axios')>();
    return { ...actual, default: { ...actual.default, create: (config: Parameters<typeof actual.default.create>[0]) =>
        actual.default.create({ ...config, adapter: async request => {
            http.requests.push({ method: request.method!.toUpperCase(), url: `${request.baseURL}${request.url}`, data: request.data });
            const reply = http.replies.shift()!;
            const response = { ...reply, headers: {}, config: request, statusText: '' };
            if (reply.status >= 400) throw new actual.AxiosError(`Request failed (${reply.status})`, undefined, request, undefined, response);
            return response;
        } }) } };
});
import { fetchState, postRun, renameChannel, deleteChannel } from './api';
import { requestJson } from './api/client';

beforeEach(() => {
    http.requests.length = 0; http.replies.length = 0;
    localStorage.setItem('apiUrl', 'https://one.example/api');
    // Shared response fixtures pin the public wire/error contracts.
    vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
        http.requests.push({ method: options?.method ?? 'GET', url: String(url), data: options?.body });
        const reply = http.replies.shift()!;
        return new Response(typeof reply.data === 'string' ? reply.data : JSON.stringify(reply.data), { status: reply.status });
    }));
});
afterEach(() => { localStorage.removeItem('apiUrl'); vi.unstubAllGlobals(); });

it('reads the current base URL on every domain request and preserves verbs/bodies __F0522_F0521', async () => {
    http.replies.push({ status: 200, data: {} }, { status: 200, data: {} }, { status: 200, data: {} }, { status: 200, data: {} });
    await fetchState();
    localStorage.setItem('apiUrl', 'https://two.example/api');
    await renameChannel('id', 'exact title');
    await deleteChannel('id');
    await requestJson('/runners', 'POST', { name: 'body' });
    expect(http.requests.map(r => [r.method, r.url])).toEqual([
        ['GET', 'https://one.example/api/state'], ['PUT', 'https://two.example/api/channels/id/rename?name=exact%20title'],
        ['DELETE', 'https://two.example/api/channels/id'], ['POST', 'https://two.example/api/runners'],
    ]);
    expect(http.requests[1].data).toBeUndefined();
    expect(JSON.parse(String(http.requests[3].data))).toEqual({ name: 'body' });
});

it('retains runner status metadata and structured or plain-text refusal wording __F0521', async () => {
    http.replies.push({ status: 422, data: { detail: [{ msg: 'field required' }] } }, { status: 500, data: '<html>proxy</html>' });
    await expect(requestJson('/runners')).rejects.toMatchObject({ message: 'field required', status: 422 });
    await expect(requestJson('/runners')).rejects.toMatchObject({ message: 'Request failed (500)', status: 500 });
});

it('retains run-start required labels and server fallback wording __F0522', async () => {
    http.replies.push({ status: 422, data: { code: 'missing_required', fields: [{ label: 'Prompt' }, { key: 'token' }] } }, { status: 503, data: '<html>proxy</html>' });
    await expect(postRun('Q&A #2', { runner: 'r', cellId: 'c' })).rejects.toThrow('Cannot run — required fields are empty: Prompt, token');
    await expect(postRun('Q&A #2', { runner: 'r', cellId: 'c' })).rejects.toThrow("Couldn't reach the server — check your connection.");
    expect(http.requests[0].url).toContain('/channels/Q%26A%20%232/runs');
});

it('sends the selected file and reads the surface and selected-file refusals (GN-01, GN-03)', async () => {
    http.replies.push(
        { status: 200, data: { run_id: 'r', status: 'queued', opens: ['_runs/x/chat/conversation.chat'] } },
        { status: 409, data: { error: 'surface_taken', owner: 'other-run', path: 'status.chat', message: 'status.chat belongs to other-run; choose another path or Run in new cell' } },
        { status: 409, data: { error: 'selected_file_missing' } },
    );
    const answer = await postRun('ch', { runner: 'r', cellId: 'c', selectedFile: 'docs/a.md' });
    expect(answer.opens).toEqual(['_runs/x/chat/conversation.chat']);
    expect(JSON.parse(String(http.requests[0].data))).toEqual({ runner: 'r', cell_id: 'c', selected_file: 'docs/a.md' });
    await expect(postRun('ch', { runner: 'r', cellId: 'c' })).rejects.toThrow('status.chat belongs to other-run');
    await expect(postRun('ch', { runner: 'r', cellId: 'c', selectedFile: 'gone.md' })).rejects.toThrow('The selected file is no longer in this cell');
});
