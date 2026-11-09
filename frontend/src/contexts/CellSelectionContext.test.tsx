import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CellSelectionContext, useCellSelected } from './CellSelectionContext';

function Rule() {
    const selected = useCellSelected();
    return <span data-testid="rule" className={selected ? 'bg-brass' : 'bg-teal'} />;
}

describe('CellSelectionContext', () => {
    it('reads false outside a provider, so nothing unselected turns brass (C10)', () => {
        render(<Rule />);
        expect(screen.getByTestId('rule').className).toBe('bg-teal');
    });

    it('the provider value reaches deep chrome, and follows a selection change (C10)', () => {
        const { rerender } = render(
            <CellSelectionContext.Provider value><div><Rule /></div></CellSelectionContext.Provider>,
        );
        expect(screen.getByTestId('rule').className).toBe('bg-brass');
        rerender(<CellSelectionContext.Provider value={false}><div><Rule /></div></CellSelectionContext.Provider>);
        expect(screen.getByTestId('rule').className).toBe('bg-teal');
    });
});
