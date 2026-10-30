import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { FieldControl } from './FieldControl';
import type { SetYamlField } from '@/lib/setYaml';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

afterEach(cleanup);

/** The retired palette and colour literals: none may come back (S04). */
const OLD_PALETTE = RETIRED_TOKENS;

const field = (type: string, extra: Partial<SetYamlField> = {}): SetYamlField =>
    ({ key: type, label: `${type} field`, type, ...extra }) as SetYamlField;

describe('FieldControl on the skin tokens (S04)', () => {
    it('renders every field type on the shared field look, never the retired palette (S04)', () => {
        const types = ['text', 'number', 'textarea', 'dropdown'] as const;
        for (const type of types) {
            const extra = type === 'dropdown' ? { options: [{ value: 'a', label: 'A' }] } : {};
            const view = render(<FieldControl field={field(type, { required: true, ...extra })} value={null} disabled={false} onChange={vi.fn()} />);
            const control = view.container.querySelector('input, select, textarea')!;
            expect(control.className).toContain('border-line');
            expect(control.className).toContain('bg-sheet');
            expect(control.className).toContain('text-ink');
            expect(control.className).toContain('focus:border-line-2');
            expect(screen.getByText('*').className).toContain('text-red');
            expect(view.container.innerHTML).not.toMatch(OLD_PALETTE);
            cleanup();
        }
    });

    it('a checkbox takes the teal on-state accent (S04)', () => {
        const view = render(<FieldControl field={field('checkbox')} value={true} disabled={false} onChange={vi.fn()} />);
        expect(screen.getByRole('checkbox').className).toBe('accent-teal');
        expect(view.container.innerHTML).not.toMatch(OLD_PALETTE);
    });

    it('a textarea keeps its fixed height: the shared field resize is overridden (S04)', () => {
        render(<FieldControl field={field('textarea')} value="x" disabled={false} onChange={vi.fn()} />);
        const area = screen.getByRole('textbox');
        expect(area.className).toContain('resize-none');
        expect(area.className).not.toContain('resize-y');
    });
});
