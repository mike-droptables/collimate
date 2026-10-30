import { describe, expect, it } from 'vitest';
import type { Account } from './accountsApi';
import type { RunnerDraft, RunnerGenerator } from './runnerApi';
import { isRunnerReady, runnerReadiness } from './runnerReadiness';

const generator: RunnerGenerator = {
    id: 'agent', name: 'Agent', main_config: 'settings.set_yaml',
    fields: [
        { key: 'backend', type: 'dropdown', label: 'Harness', default: 'pydantic-ai', account_role: 'model', account_property: 'metadata.harness' },
        { key: 'mode', type: 'dropdown', label: 'Mode', options: [{ value: 'basic', label: 'Basic' }, { value: 'advanced', label: 'Advanced', enable_if: { backend: 'codex' } }] },
        { key: 'title', type: 'text', label: 'Title', required: true },
    ],
    settings_files: {}, config_folders: [], enforces: [], prompt_required: true,
    account_requirements: {
        model: { label: 'Agent model', kind: 'model', capabilities: ['text'] },
        git: { label: 'Git account', kind: 'git', required: false },
    },
    configs: [], default: { runner: { name: '', description: '', tags: [], prompt: '', settings: {}, accounts: {}, folders: [], permissions: {}, packages: {} }, files: [] },
};

const draft = (patch: Partial<RunnerDraft['runner']> = {}): RunnerDraft => ({
    runner: { id: 'agent-1', generator: 'agent', name: 'Agent', description: '', tags: [], prompt: '', settings: { 'settings.set_yaml': {} }, accounts: {}, folders: [], permissions: {}, packages: {}, ...patch },
    files: [],
});

const model: Account = { id: 'fast', name: 'Fast', kind: 'model', service: 'openai', protocol: 'openai', endpoint: '', model: 'fast', capabilities: ['text'], auth_type: 'api_key', metadata: { harness: 'pydantic-ai' }, configured: true };

describe('runner readiness', () => {
    it('lists the unbound required role, the required field and the prompt, but not an optional role', () => {
        const readiness = runnerReadiness(draft(), generator, []);
        expect(readiness).toEqual({ missing: ['Agent model', 'Title', 'Prompt'], invalid: [] });
        expect(isRunnerReady(readiness)).toBe(false);
    });

    it('is ready once the role is bound to a matching account and the values are set', () => {
        const readiness = runnerReadiness(draft({ accounts: { model: 'fast' }, prompt: 'Go', settings: { 'settings.set_yaml': { title: 'T', mode: 'basic' } } }), generator, [model]);
        expect(readiness).toEqual({ missing: [], invalid: [] });
        expect(isRunnerReady(readiness)).toBe(true);
    });

    it('counts a binding to a missing or no-longer-matching account as unset', () => {
        const ready = { prompt: 'Go', settings: { 'settings.set_yaml': { title: 'T' } } };
        expect(runnerReadiness(draft({ ...ready, accounts: { model: 'gone' } }), generator, [model]).missing).toEqual(['Agent model']);
        expect(runnerReadiness(draft({ ...ready, accounts: { model: 'fast' } }), generator, [{ ...model, configured: false }]).missing).toEqual(['Agent model']);
        expect(runnerReadiness(draft({ ...ready, accounts: { model: 'fast', git: 'gone' } }), generator, [model]).missing).toEqual(['Git account']);
    });

    it('reads account-derived values, so an option the bound harness disables is invalid', () => {
        const advanced = draft({ accounts: { model: 'fast' }, prompt: 'Go', settings: { 'settings.set_yaml': { title: 'T', mode: 'advanced' } } });
        expect(runnerReadiness(advanced, generator, [model])).toEqual({ missing: [], invalid: ['Mode'] });
        const codex = { ...model, metadata: { harness: 'codex' } };
        expect(runnerReadiness(advanced, generator, [codex])).toEqual({ missing: [], invalid: [] });
    });

    it('needs no prompt where the gate leaves it optional', () => {
        const optional = { ...generator, prompt_required: false };
        expect(runnerReadiness(draft({ accounts: { model: 'fast' }, settings: { 'settings.set_yaml': { title: 'T' } } }), optional, [model]).missing).toEqual([]);
    });
});
