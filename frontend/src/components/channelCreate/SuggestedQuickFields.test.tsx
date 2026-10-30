import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { SuggestedQuickFields } from './SuggestedQuickFields';
import type { RunnerDraft, RunnerGenerator } from '@/lib/runnerApi';

const template = { name: '', description: '', tags: [], prompt: '', settings: { 'settings.set_yaml': {} }, accounts: {}, folders: [], permissions: {}, packages: {} };
const generator: RunnerGenerator = {
    id: 'git', name: 'Git', main_config: 'settings.set_yaml', fields: [], settings_files: {}, config_folders: [], enforces: [],
    account_requirements: {}, prompt_required: false,
    prompt_intent: "The repository: its address (https://host/path, ssh://user@host/path or user@host:path), or owner/name on the bound GitHub or GitLab account's server",
    configs: [{ id: 'default', name: 'Clone / Update' }], default: { runner: template, files: [] },
};
const draft: RunnerDraft = { runner: { ...template, id: 'git-sync', generator: 'git', name: 'Git Clone / Update' }, files: [] };
const quick = (over: Partial<RunnerGenerator> = {}) => render(<SuggestedQuickFields draft={draft} generator={{ ...generator, ...over }}
    fields={[{ prompt: true }]} accounts={[]} disabled={false} onChange={vi.fn()} onOpenKeys={vi.fn()} />);

afterEach(cleanup);

describe('suggested quick fields', () => {
    it('prompt quick field describes the generator\'s prompt_intent', () => {
        quick();
        const prompt = screen.getByRole('textbox', { name: /^Prompt/ });
        const hint = screen.getByText(generator.prompt_intent!);
        expect(prompt.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(prompt.closest('div')).toContainElement(hint);
    });

    it('prompt quick field shows no hint when the generator declares no prompt_intent', () => {
        const { container } = quick({ prompt_intent: undefined });
        expect(screen.getByRole('textbox', { name: /^Prompt/ })).toBeInTheDocument();
        expect(container.querySelector('.text-ink-3')).toBeNull();
    });
});
