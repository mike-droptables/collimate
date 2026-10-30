import { afterEach, expect, it } from 'vitest';
import { FIXTURE_BASIC_ID, MOUNTED_TEXT, rendererFixtureSource } from '@/test/rendererFixtures';

// public/sandbox.html reads the file's bytes from the `hash` URL before it
// mounts the renderer. A read that fails (a revoked blob) must never mount
// the renderer over an empty, editable buffer: the frame says why instead.
// An empty `hash` (the pool's warm-up) reads nothing and mounts with ''.

const opened: Array<() => void> = [];
afterEach(() => { for (const close of opened.splice(0)) close(); });

/** A bare sandbox frame whose bundle requests this test answers with the
 *  basic fixture (it prints MOUNTED_TEXT and the content it was given). */
function openSandbox(hash: string) {
    const frame = document.createElement('iframe');
    let bundleRequests = 0;
    const answer = (event: MessageEvent) => {
        if (event.source !== frame.contentWindow || event.data?.type !== 'RENDERER_BUNDLE_REQUEST') return;
        bundleRequests++;
        frame.contentWindow!.postMessage({
            type: 'RENDERER_BUNDLE_RESULT', token: event.data.token, code: rendererFixtureSource(FIXTURE_BASIC_ID),
        }, location.origin);
    };
    window.addEventListener('message', answer);
    frame.src = `/sandbox.html?renderer=${FIXTURE_BASIC_ID}&hash=${encodeURIComponent(hash)}`;
    document.body.append(frame);
    opened.push(() => { window.removeEventListener('message', answer); frame.remove(); });
    return {
        mountText: () => frame.contentDocument?.getElementById('mount')?.textContent ?? '',
        bundleRequests: () => bundleRequests,
    };
}

it('a failed content read shows why and mounts no renderer', async () => {
    const revoked = URL.createObjectURL(new Blob(['the file body'], { type: 'text/plain' }));
    URL.revokeObjectURL(revoked);
    const sandbox = openSandbox(revoked);
    await expect.poll(sandbox.mountText, { timeout: 10000 })
        .toContain("Failed to load renderer: could not read the file's content");
    expect(sandbox.mountText()).not.toContain(MOUNTED_TEXT);
    expect(sandbox.bundleRequests()).toBe(0);
}, 15000);

it('an empty hash reads nothing and mounts with an empty file', async () => {
    const sandbox = openSandbox('');
    await expect.poll(sandbox.mountText, { timeout: 10000 }).toBe(`${MOUNTED_TEXT}:`);
}, 15000);

it('a readable hash mounts with its bytes', async () => {
    const live = URL.createObjectURL(new Blob(['the file body'], { type: 'text/plain' }));
    try {
        const sandbox = openSandbox(live);
        await expect.poll(sandbox.mountText, { timeout: 10000 }).toBe(`${MOUNTED_TEXT}:the file body`);
    } finally {
        URL.revokeObjectURL(live);
    }
}, 15000);
