// Whether a runner draft can run: every required account role bound to an
// account that still matches it, every required field set, every option
// valid, and the prompt present when the generator's gate requires it. The
// browser-side mirror of the server's readiness check (validate_definition
// over the account-derived settings), so a list of drafts re-evaluates the
// moment an account is bound or the Key manager adds one.

import { accountMatches, accountSettings, type Account } from './accountsApi';
import { promptGate, type RunnerDraft, type RunnerGenerator } from './runnerApi';
import { configViolations, normalizeOptions } from './setYaml';

export interface RunnerReadiness {
    /** Labels of what is unset: account roles, required fields, the prompt. */
    missing: string[];
    /** Labels of fields whose value is not one of their options. */
    invalid: string[];
}

/** What keeps `draft` from running against `generator` with `accounts`;
 *  both lists empty means it is ready. */
export function runnerReadiness(draft: RunnerDraft, generator: RunnerGenerator, accounts: Account[]): RunnerReadiness {
    const { runner } = draft;
    const values = accountSettings(generator, runner.accounts, accounts, runner.settings[generator.main_config] ?? {});
    const fields = generator.fields.map(field => ({ ...field, options: normalizeOptions(field.options) }));
    const issues = configViolations({ fields, values });
    const unresolvedRoles = Object.entries(generator.account_requirements).filter(([role, requirement]) => {
        const selected = runner.accounts[role];
        return selected ? !accounts.some(account => account.id === selected && accountMatches(account, requirement)) : requirement.required !== false;
    }).map(([role, requirement]) => requirement.label ?? role);
    return {
        missing: [
            ...unresolvedRoles,
            ...issues.missingRequired.map(field => field.label),
            ...(promptGate(generator, values).required && !runner.prompt.trim() ? ['Prompt'] : []),
        ],
        invalid: issues.invalidOptions.map(field => field.label),
    };
}

/** Whether nothing keeps the draft from running. */
export const isRunnerReady = (readiness: RunnerReadiness): boolean => readiness.missing.length === 0 && readiness.invalid.length === 0;
