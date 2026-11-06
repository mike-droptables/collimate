import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { ChannelReadOnlyContext } from '@/contexts/ChannelReadOnlyContext';
import type { Cell } from '@/lib/types';

// An archived channel opens read-only: ChannelReadOnlyContext removes the
// cell's editing chrome and locks the editor.
const editorProps = vi.hoisted(() => ({ current: null as null | { readOnly?: boolean } }));
vi.mock('./CellEditor', () => ({
    default: (p: { readOnly?: boolean }) => { editorProps.current = p; return <div data-testid="cell-editor" />; },
}));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));

import { StackItem } from './StackItem';

const cell = (): Cell => ({ id: 'cell-1', name: 'Old name', files: [fileFixture('notes.md', 'body', { language: 'plaintext' })], channel: 'ch' });

type Props = React.ComponentProps<typeof StackItem>;
const mount = (readOnly: boolean | undefined, over: Partial<Props> = {}) => {
    const item = <StackItem stackId="s1" cell={cell()} index={0} isFront isSelected viewMode="stack" width={600} height={400}
        focusArea="main" onSelect={vi.fn()} onUpdate={vi.fn()} onCellDragStart={vi.fn()} onDuplicateToNewStack={vi.fn()}
        onRemoveCell={vi.fn()} {...over} />;
    return render(readOnly === undefined ? item
        : <ChannelReadOnlyContext.Provider value={readOnly}>{item}</ChannelReadOnlyContext.Provider>);
};

describe('StackItem in an archived channel', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        editorProps.current = null;
        useCollimateStore.setState({
            cellCache: {}, runs: {}, stoppingRunIds: new Set(), runPhases: {}, runLogs: {},
            lastFinishedByCell: {}, cellLayouts: {}, state: null, pendingSelectCellId: null,
        } as never);
    });

    it('without the context the cell keeps Run, the editable title, Move, Duplicate, Remove and an editable editor (plan 1105 P7)', () => {
        mount(undefined);
        expect(screen.getByTestId('cell-run')).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Old name' })).toBeTruthy();
        expect(screen.getByLabelText('Move cell')).toBeTruthy();
        expect(screen.getByLabelText('Duplicate cell')).toBeTruthy();
        expect(screen.getByTestId('cell-remove')).toBeTruthy();
        expect(editorProps.current?.readOnly).toBeFalsy();
    });

    it('a provider of false changes nothing (plan 1105 P7)', () => {
        mount(false);
        expect(screen.getByTestId('cell-run')).toBeTruthy();
        expect(screen.getByTestId('cell-remove')).toBeTruthy();
        expect(editorProps.current?.readOnly).toBeFalsy();
    });

    it('with the context true there is no Run, Move, Duplicate or Remove, the title is plain text and the editor is read-only (plan 1105 P7)', () => {
        mount(true);
        expect(screen.queryByTestId('cell-run')).toBeNull();
        expect(screen.queryByLabelText('Move cell')).toBeNull();
        expect(screen.queryByLabelText('Duplicate cell')).toBeNull();
        expect(screen.queryByTestId('cell-remove')).toBeNull();
        expect(screen.queryByRole('button', { name: 'Old name' })).toBeNull();
        const title = screen.getByText('Old name');
        expect(title.tagName).toBe('SPAN');
        expect(editorProps.current?.readOnly).toBe(true);
    });

    it('with the context true a Capabilities cell has no Hide (plan 1105 P7)', () => {
        mount(undefined, { capabilitiesCell: true, onHideStack: vi.fn() });
        expect(screen.getByTestId('cell-hide-stack')).toBeTruthy();
        cleanup();
        mount(true, { capabilitiesCell: true, onHideStack: vi.fn() });
        expect(screen.queryByTestId('cell-hide-stack')).toBeNull();
    });
});
