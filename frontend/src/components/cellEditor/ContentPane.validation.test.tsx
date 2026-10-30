import { fileFixture } from '@/test/fileFixtures';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { CellValidation } from '@/lib/definitionApi';
import type { Cell } from '@/lib/types';
import { createDefaultLayout } from '@/lib/cellLayout';

const state = vi.hoisted(() => ({ validation: null as CellValidation | null }));
vi.mock('@/hooks/useCellValidation', () => ({ useCellValidation: () => state.validation }));
vi.mock('./TabStrip', () => ({ TabStrip: () => null }));
vi.mock('./TabBody', () => ({
    TabBody: ({ validationVerdict }: { validationVerdict?: { valid: boolean; error?: string } }) =>
        <output data-testid="current-verdict">{validationVerdict?.error ?? (validationVerdict?.valid ? 'passed' : '')}</output>,
}));
vi.mock('@/contexts/GuestPoolContext', () => ({ useGuestPool: () => ({ containerOf: () => null }) }));

import { ContentPane } from './ContentPane';

afterEach(() => { cleanup(); state.validation = null; });

it('shows only a current live-file verdict in the opened renderer tab', () => {
    const file = fileFixture('note.txt', 'body', { language: 'text', timestamp: '' });
    const cell = { id: 'cell', name: 'Cell', pinned: 'note.txt', files: [file] } as Cell;
    const layout = createDefaultLayout({ filePaths: ['note.txt'], pinnedPath: 'note.txt' });
    const props = {
        cell, layout, filesPane: layout.filesPane, channel: 'channel',
        onLayoutChange: () => {}, isActive: true, visible: true, readOnly: false,
        registerEditorHandle: () => {}, onBeforeStructuralChange: () => {},
        dirtyByName: {}, lastSavedByName: {}, onToggleMaximized: () => {},
        onCloseFileTab: () => {}, onCloseTabById: () => {}, onUpdateFiles: () => {},
        isZoomTransitioning: false,
    };
    state.validation = {
        cell_id: 'cell', source_version_id: 'version', source_revision: 'revision', pending: false,
        verdicts: { 'note.txt': { valid: false, error: 'Missing required heading' } },
    };
    const view = render(<ContentPane {...props} />);
    expect(screen.getByTestId('current-verdict')).toHaveTextContent('Missing required heading');

    view.rerender(<ContentPane {...props} dirtyByName={{ 'note.txt': true }} />);
    expect(screen.getByTestId('current-verdict')).toBeEmptyDOMElement();

    state.validation = { ...state.validation, pending: true };
    view.rerender(<ContentPane {...props} />);
    expect(screen.getByTestId('current-verdict')).toBeEmptyDOMElement();

    state.validation = { ...state.validation, pending: false, verdicts: { 'note.txt': { valid: true } } };
    view.rerender(<ContentPane {...props} />);
    expect(screen.getByTestId('current-verdict')).toHaveTextContent('passed');
});
