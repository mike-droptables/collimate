// The Editing section: one labelled switch, Spellcheck, bound to the
// channel's `editing` section, in the Commands pane's 320px column. It shows
// the adopted value, a toggle saves only that section to the channel the doc
// came from, and a failed save rolls the switch back and says why.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

import { putChannelSettings } from '@/lib/api';
import { EditingSection } from './EditingSection';
import { expectButtonRowsWrap, expectFitsPaneColumn, renderInPaneColumn } from './paneColumn.testkit';
import { useEditingStore } from '@/stores/useEditingStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { retiredOr } from '@/test/retiredTokens';

const putMock = vi.mocked(putChannelSettings);
const spellcheck = () => screen.getByRole('switch', { name: 'Spellcheck' });

beforeEach(() => {
    useEditingStore.getState().adoptEditing({ spellcheck: true });
    useChannelSettingsStore.setState({ loadedFor: '11111111-1111-4111-8111-111111111111' });
    putMock.mockClear();
});
afterEach(() => {
    cleanup();
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
});

describe('EditingSection', () => {
    it('fits the pane\'s column with no heading or description of its own: the pane\'s section head carries both (CP-6)', () => {
        const { body } = renderInPaneColumn(<EditingSection />);
        expect(screen.queryByRole('heading')).toBeNull();
        expect(screen.queryByText(/every renderer/)).toBeNull();
        expect(body.querySelector('.grid')).toBeNull();
        expectFitsPaneColumn(body);
        expectButtonRowsWrap(body);
        expect(spellcheck().closest('.border-line')).toHaveTextContent('Underline misspelled words');
    });

    it('reflects the adopted section and paints the switch from the skin tokens (CP-6)', () => {
        const { container } = renderInPaneColumn(<EditingSection />);
        expect(spellcheck()).toHaveAttribute('aria-checked', 'true');
        expect(spellcheck()).toHaveClass('bg-ink');
        act(() => useEditingStore.getState().adoptEditing({ spellcheck: false }));
        expect(spellcheck()).toHaveAttribute('aria-checked', 'false');
        expect(spellcheck()).toHaveClass('bg-line-2');
        const painted = [...container.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('var\\(--', 'rgba?\\('));
    });

    it('a toggle PUTs only {editing: {spellcheck: false}} to the loaded channel and turns <html spellcheck> off (CP-6)', async () => {
        renderInPaneColumn(<EditingSection />);
        await act(async () => { fireEvent.click(spellcheck()); });
        expect(putMock).toHaveBeenCalledExactlyOnceWith('11111111-1111-4111-8111-111111111111', { editing: { spellcheck: false } });
        expect(spellcheck()).toHaveAttribute('aria-checked', 'false');
        expect(document.documentElement.getAttribute('spellcheck')).toBe('false');
        expect(screen.getByText('Saved')).toBeDefined();
    });

    it('a failed save rolls the switch back and shows the error (CP-6)', async () => {
        putMock.mockRejectedValueOnce(new Error('HTTP 503'));
        renderInPaneColumn(<EditingSection />);
        await act(async () => { fireEvent.click(spellcheck()); });
        expect(spellcheck()).toHaveAttribute('aria-checked', 'true');
        expect(document.documentElement.getAttribute('spellcheck')).toBe('true');
        expect(screen.getByRole('alert').textContent).toBe('HTTP 503');
    });
});
