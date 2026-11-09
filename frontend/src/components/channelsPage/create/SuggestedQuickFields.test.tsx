import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { SuggestedQuickFields } from './SuggestedQuickFields';
import type { RunnerDraft, RunnerGenerator } from '@/lib/runnerApi';

const template = { name: '', description: '', tags: [], prompt: '', settings: { 'settings.set_yaml': {} }, accounts: {}, folders: [], permissions: {}, packages: {} };
const generator: RunnerGenerator = {
    id: 'synth-fetch', name: 'Synth fetch', main_config: 'settings.set_yaml', fields: [], settings_files: {}, config_folders: [], enforces: [],
    account_requirements: {}, prompt_required: false, prompt_label: 'Prompt', prompt_type: 'textarea',
    prompt_intent: 'What to fetch, as an address or a short name',
    configs: [{ id: 'default', name: 'Default' }], default: { runner: template, files: [] },
};
const draft: RunnerDraft = { runner: { ...template, id: 'synth-fetch-1', generator: 'synth-fetch', name: 'Synth fetch' }, files: [] };
const quick = (over: Partial<RunnerGenerator> = {}, prompt = '', onChange = vi.fn()) => render(<SuggestedQuickFields
    draft={{ ...draft, runner: { ...draft.runner, prompt } }} generator={{ ...generator, ...over }}
    fields={[{ prompt: true }]} accounts={[]} disabled={false} onChange={onChange} onOpenKeys={vi.fn()} />);

afterEach(cleanup);

describe('suggested quick fields', () => {
    it('prompt quick field describes the generator\'s prompt_intent', () => {
        quick();
        const prompt = screen.getByRole('textbox', { name: 'Prompt' });
        expect(prompt.tagName).toBe('TEXTAREA');
        const hint = screen.getByText(generator.prompt_intent!);
        expect(prompt.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(prompt).toHaveAccessibleDescription(generator.prompt_intent!);
    });

    it('prompt quick field shows no hint when the generator declares no prompt_intent', () => {
        const { container } = quick({ prompt_intent: undefined });
        expect(screen.getByRole('textbox', { name: 'Prompt' })).not.toHaveAttribute('aria-describedby');
        expect(container.querySelector('.text-ink-3')).toBeNull();
    });

    it('prompt quick field takes the generator\'s label and its single-line shape', () => {
        const onChange = vi.fn();
        quick({ prompt_label: 'Address', prompt_type: 'text', prompt_required: true }, '', onChange);
        const input = screen.getByRole('textbox', { name: 'Address*' });
        expect(input.tagName).toBe('INPUT');
        expect(screen.queryByRole('textbox', { name: /^Prompt/ })).toBeNull();
        fireEvent.change(input, { target: { value: 'host/path' } });
        expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ prompt: 'host/path' }) }));
    });

    it('a saved value with line breaks keeps the several-line box', () => {
        quick({ prompt_label: 'Address', prompt_type: 'text' }, 'one\ntwo');
        expect(screen.getByRole('textbox', { name: 'Address' }).tagName).toBe('TEXTAREA');
    });

    it('deleting the last line break keeps the same box for the rest of the edit', () => {
        const over = { prompt_label: 'Repository', prompt_type: 'text' as const };
        const view = quick(over, 'owner/name\n');
        const box = screen.getByRole('textbox', { name: 'Repository' });
        view.rerender(<SuggestedQuickFields draft={{ ...draft, runner: { ...draft.runner, prompt: 'owner/name' } }} generator={{ ...generator, ...over }}
            fields={[{ prompt: true }]} accounts={[]} disabled={false} onChange={vi.fn()} onOpenKeys={vi.fn()} />);
        expect(screen.getByRole('textbox', { name: 'Repository' })).toBe(box);
        expect(box.tagName).toBe('TEXTAREA');
    });
});
