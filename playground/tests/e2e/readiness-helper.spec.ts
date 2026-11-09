import {expect, test} from '@playwright/test';
import {waitForPlaygroundReady} from './helpers/playground';

// Real browser polling against a deliberately unready page: no server/runtime
// substitute can turn the original helper's ignored timeout into this result.
test('readiness obeys its short timeout and reports the last stage __P4F09', async ({page}) => {
    test.setTimeout(1500);
    await page.setContent(`<div id="collimate-playground-overlay"><div>
        <div>Starting the browser demo</div><div>Waiting</div><div></div>
        <div><div>Installing FastAPI and pydantic…</div><div>0s</div></div>
    </div></div>`);
    await expect(waitForPlaygroundReady(page, 100)).rejects.toThrow(
        /Timeout 100ms exceeded.*last boot stage: Installing FastAPI and pydantic…/s,
    );
});

test('readiness preserves an actual boot failure instead of waiting for ready __P4F09', async ({page}) => {
    test.setTimeout(1500);
    await page.setContent(`<div id="collimate-playground-overlay"><div>
        <div>Starting the browser demo</div><div>Waiting</div><div></div>
        <div><div>Preparing file verification…</div><div>0s</div></div>
    </div></div>`);
    // The production boot catch reports the thrown error through this existing
    // overlay. Trigger it after polling starts, rather than only checking markup.
    await page.evaluate(() => setTimeout(() => {
        const card = document.getElementById('collimate-playground-overlay')!.firstElementChild!;
        const error = new Error('Verifier timed out');
        console.error('[collimate]', 'boot failed:', error);
        card.children[0].textContent = 'The demo failed to start';
        card.children[1].textContent = error.message;
    }, 30));
    const began = Date.now();
    await expect(waitForPlaygroundReady(page, 1000)).rejects.toThrow(
        'Boot failed: Verifier timed out; last boot stage: Preparing file verification…',
    );
    expect(Date.now() - began).toBeLessThan(750);
});
