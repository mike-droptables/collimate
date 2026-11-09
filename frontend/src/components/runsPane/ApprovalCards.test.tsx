import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { ApprovalCards } from './ApprovalCards';

afterEach(cleanup);

describe('ApprovalCards', () => {
    it('renders nothing with no cards', () => {
        const { container } = render(<ApprovalCards cards={[]} onAnswer={() => true} />);
        expect(container.innerHTML).toBe('');
    });

    it('shows each card\'s title, detail and options (allow kinds primary), and answers through onAnswer', () => {
        const onAnswer = vi.fn(() => true);
        render(<ApprovalCards onAnswer={onAnswer} cards={[
            { id: 'p1', title: 'Run a command', kind: 'execute', detail: 'rm -rf build', options: [
                { option_id: 'allow', name: 'Allow', kind: 'allow_once' }, { option_id: 'deny', name: '', kind: 'reject_once' }] },
            { id: 'p2', title: 'Edit a file', kind: 'edit', options: [] },
        ]} />);
        const [first, second] = screen.getAllByTestId('approval-card');
        expect(first).toHaveAttribute('data-id', 'p1');
        expect(first.className).toContain('border-l-brass');
        expect(within(first).getByText('Run a command').className).toContain('font-semibold');
        expect(within(first).getByText('rm -rf build').tagName).toBe('PRE');
        const [allow, deny] = within(first).getAllByTestId('approval-option');
        expect(allow.textContent).toBe('Allow');
        expect(allow.className).toContain('font-semibold');
        expect(deny.textContent).toBe('deny');
        expect(deny.className).not.toContain('font-semibold');
        fireEvent.click(allow);
        expect(onAnswer).toHaveBeenCalledWith('p1', 'allow');
        fireEvent.click(within(second).getByTestId('approval-cancel'));
        expect(onAnswer).toHaveBeenCalledWith('p2', null);
        expect(second.querySelector('pre')).toBeNull();
    });
});
