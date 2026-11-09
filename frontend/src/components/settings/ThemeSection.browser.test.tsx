/**
 * The theme editor laid out for real in the Commands pane's 352px column:
 * the Background card's Image row (thumbnail, Choose… or Change… and
 * Remove, Uploading… while a picture goes up) fits the card at every state,
 * so nothing pushes the card past its right border.
 */
import { afterEach, expect, it, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { render } from 'vitest-browser-react';
import '@/app/globals.css';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
    fetchChannelSettings: vi.fn(),
    fetchAppSettings: vi.fn(() => new Promise(() => {})),
    putAppSettings: vi.fn(() => Promise.resolve({})),
}));
vi.mock('@/lib/api/appSettings', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/appSettings')>()),
    uploadThemeImage: vi.fn(),
}));

import { uploadThemeImage } from '@/lib/api/appSettings';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { writeBootCache } from '@/lib/settingsCache';
import { COMMAND_PANE_WIDTH } from '@/stores/collimate/commandPaneSlice';
import { ThemeSection } from './ThemeSection';

afterEach(() => {
    vi.mocked(uploadThemeImage).mockReset();
    localStorage.clear();
});

/** Nothing inside `el` reaches past its right edge. */
function expectNoHorizontalOverflow(el: HTMLElement, name: string) {
    expect(el.scrollWidth, `${name} scrollWidth`).toBeLessThanOrEqual(el.clientWidth);
}

/** `inner` lies within `outer`'s box, left to right. */
function expectWithin(inner: Element, outer: Element, name: string) {
    const a = inner.getBoundingClientRect();
    const b = outer.getBoundingClientRect();
    expect(a.left, `${name} left`).toBeGreaterThanOrEqual(b.left - 0.5);
    expect(a.right, `${name} right`).toBeLessThanOrEqual(b.right + 0.5);
}

it('the Image row fits the 352px column with no picture, while uploading and with a picture set', async () => {
    writeBootCache({ themeLibrary: { defaultThemeId: 'night', userThemes: [] } });
    const view = await render(
        <div data-pane-column style={{ width: `${COMMAND_PANE_WIDTH}px` }}>
            <div data-pane-body className="min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
                <ThemeProvider><ThemeSection mode="default" /></ThemeProvider>
            </div>
        </div>,
    );
    try {
        await userEvent.click(page.getByRole('button', { name: /new theme/i }));
        const card = page.getByRole('region', { name: 'Theme editor' }).element() as HTMLElement;
        const fieldset = [...card.querySelectorAll('fieldset')].find(f => f.querySelector('legend')?.textContent === 'Background')!;
        const input = fieldset.querySelector('input[aria-label="Choose image"]') as HTMLInputElement;
        const row = input.closest('[data-image-row]') as HTMLElement;
        expect(row).not.toBeNull();
        const body = document.querySelector('[data-pane-body]') as HTMLElement;

        const check = (state: string) => {
            expectNoHorizontalOverflow(body, `${state}: pane body`);
            expectNoHorizontalOverflow(card, `${state}: card`);
            expectNoHorizontalOverflow(fieldset, `${state}: Background`);
            expectNoHorizontalOverflow(row, `${state}: Image row`);
            expectWithin(fieldset, card, `${state}: Background`);
            expectWithin(row, fieldset, `${state}: Image row`);
            for (const button of row.querySelectorAll('button')) {
                expectWithin(button, row, `${state}: ${button.textContent}`);
                expect(button.scrollWidth, `${state}: ${button.textContent} shows its whole label`).toBeLessThanOrEqual(button.clientWidth);
            }
        };

        // No picture: the dashed box and Choose….
        await expect.element(page.getByRole('button', { name: 'Choose…' })).toBeVisible();
        check('no picture');

        // Uploading: the button says so until the store answers.
        const hash = 'e'.repeat(64);
        let answer!: (value: { blob_hash: string; size: number; encoding: string }) => void;
        vi.mocked(uploadThemeImage).mockImplementation(() => new Promise(resolve => { answer = resolve as typeof answer; }) as never);
        const png = new File([new Uint8Array([137, 80, 78, 71])], 'ground.png', { type: 'image/png' });
        await userEvent.upload(input, png);
        await expect.element(page.getByRole('button', { name: 'Uploading…' })).toBeDisabled();
        check('uploading');

        // A picture set: thumbnail, Change… and Remove, both reachable.
        answer({ blob_hash: hash, size: 4, encoding: 'binary' });
        await expect.element(page.getByRole('button', { name: 'Change…' })).toBeVisible();
        await expect.element(page.getByRole('button', { name: 'Remove' })).toBeVisible();
        expect(row.querySelector('[data-scene-image]')).not.toBeNull();
        check('picture set');

        // Changing a set picture: Uploading… and Remove together.
        vi.mocked(uploadThemeImage).mockImplementation(() => new Promise(resolve => { answer = resolve as typeof answer; }) as never);
        await userEvent.upload(input, png);
        await expect.element(page.getByRole('button', { name: 'Uploading…' })).toBeDisabled();
        check('replacing');
        answer({ blob_hash: hash, size: 4, encoding: 'binary' });
        await expect.element(page.getByRole('button', { name: 'Change…' })).toBeVisible();

        // Its siblings keep their rhythm: the Ground row's height, one line.
        const ground = fieldset.querySelector('input[name="scene.base"]')!.parentElement!;
        const groundHeight = ground.getBoundingClientRect().height;
        await userEvent.click(page.getByRole('button', { name: 'Remove' }));
        await expect.element(page.getByRole('button', { name: 'Choose…' })).toBeVisible();
        expect(row.getBoundingClientRect().height).toBeCloseTo(groundHeight, 0);
        check('removed');
    } finally {
        await view.unmount();
    }
});
